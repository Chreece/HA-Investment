"""Final purchase constraints on the complete, cash-funded portfolio.

The budget is cash designated for purchases, including any unspent prior
contributions the caller reoffers. ``existing_cash`` excludes that budget and
is not made available to the purchase allocator. Existing holdings are never
sold or resized here. All suggested purchases are bounded by their incoming
principal and quantity ceilings, including when an existing limit is breached.

Historical risk replays today's fixed weights over aligned past observations;
it is not the investor's realized track record or a forecast of future losses.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
import math
from typing import Any

from .execution_costs import apply_execution_costs
from .purchase_search import (
    prepare_whole_search,
    proposed_rows,
    risk_evaluation_limit,
    search_metadata,
)
from .risk_evidence import dated_portfolio_returns, evaluate_risk_evidence
from .validated_model import (
    ECONOMIC_SLEEVE_CAPS,
    PORTFOLIO_VOL_TARGET,
    PORTFOLIO_WEEKLY_ES95_TARGET,
    RISK_TOLERANCE,
    _candidate_evidence_eligible,
    risk_signature,
)

CONTRACT_VERSION = "complete-portfolio-confirmed-costs-v1"
_METRICS = ("annualized_volatility_3y", "expected_shortfall_95_weekly_3y", "max_drawdown_3y")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _instrument_key(row: dict[str, Any]) -> str:
    # Independently reviewed identifiers aggregate duplicate fund positions.
    # Other holdings retain their exact provider identity; names are not proof.
    identity = row.get("instrument_identity") or {}
    isin = identity.get("isin") if isinstance(identity, dict) else None
    if isin and row.get("instrument_identity_eligible") is True:
        return f"isin:{isin}"
    return f"provider:{row.get('provider', '')}:{row.get('provider_id') or row.get('symbol', '')}"


def _cash_signature() -> dict[str, Any]:
    return {
        "annualized_volatility_3y": 0.0,
        "expected_shortfall_95_weekly_3y": 0.0,
        "max_drawdown_3y": 0.0,
        "weekly_observations_3y": 0,
        "cash_only": True,
    }


def _snapshot(
    values: list[tuple[dict[str, Any], float]],
    cash: float,
    *,
    as_of: date | datetime | str | None,
    require_drawdown: bool,
) -> tuple[dict[str, Any], list[tuple[dict[str, Any], float]], list[str]]:
    try:
        total = math.fsum(value for _, value in values) + cash
    except (OverflowError, ValueError):
        return {"portfolio_value": None}, [], ["portfolio_value_invalid"]
    if not math.isfinite(total) or total < 0.0 or not math.isfinite(cash) or cash < -1e-8:
        return {"portfolio_value": None}, [], ["portfolio_value_invalid"]
    weighted = [(row, value / total) for row, value in values if value > 0.0] if total > 0 else []
    issues: list[str] = []
    if weighted:
        dated = dated_portfolio_returns(weighted, as_of=as_of)
        if dated["status"] != "ok":
            issues.extend(dated.get("issues") or ["portfolio_aligned_history_unavailable"])
        if require_drawdown and dated.get("history", {}).get("calendar_gap_count", 0):
            issues.append("portfolio_drawdown_calendar_gaps")
        signature = risk_signature(weighted)
        if any(_number(signature.get(key)) is None for key in _METRICS[:2]):
            issues.append("portfolio_aligned_history_unavailable")
        # Disconnected observations cannot establish a cumulative wealth path.
        if dated.get("history", {}).get("calendar_gap_count", 0):
            signature["max_drawdown_3y"] = None
    else:
        signature = _cash_signature()
    sleeves: dict[str, float] = defaultdict(float)
    instruments: dict[str, float] = defaultdict(float)
    for row, weight in weighted:
        sleeve = str(row.get("economic_sleeve") or "unknown")
        if sleeve not in ECONOMIC_SLEEVE_CAPS["medium"]:
            issues.append("portfolio_economic_exposure_unknown")
        sleeves[sleeve] += weight
        instruments[_instrument_key(row)] += weight
    return {
        **signature,
        "portfolio_value": total,
        "cash": max(0.0, cash),
        "cash_weight": max(0.0, cash) / total if total > 0.0 else 1.0,
        "sleeve_weights": dict(sleeves),
        "instrument_weights": dict(instruments),
    }, weighted, list(dict.fromkeys(issues))


def _breaches(snapshot: dict[str, Any], limits: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for key in _METRICS:
        ceiling = limits.get(key)
        if ceiling is None:
            continue
        value = _number(snapshot.get(key))
        if value is None:
            issues.append(f"{key}_unknown")
        elif value > ceiling + 1e-12:
            issues.append(key)
    for sleeve, value in (snapshot.get("sleeve_weights") or {}).items():
        if sleeve not in limits["sleeves"] or value > limits["sleeves"].get(sleeve, 0.0) + 1e-12:
            issues.append(f"sleeve:{sleeve}")
    cap = limits.get("instrument_fraction")
    if cap is not None:
        for key, value in (snapshot.get("instrument_weights") or {}).items():
            if value > cap + 1e-12:
                issues.append(f"instrument:{key}")
    return issues


def _non_worsening(post: dict[str, Any], baseline: dict[str, Any], limits: dict[str, Any]) -> bool:
    """Each existing breach may stay unchanged or improve; no new breach."""
    for key in _METRICS:
        ceiling = limits.get(key)
        if ceiling is None:
            continue
        before, after = _number(baseline.get(key)), _number(post.get(key))
        if before is None or after is None or after > max(ceiling, before) + 1e-12:
            return False
    for sleeve, after in (post.get("sleeve_weights") or {}).items():
        if sleeve not in limits["sleeves"]:
            return False
        before = baseline.get("sleeve_weights", {}).get(sleeve, 0.0)
        if after > max(limits["sleeves"][sleeve], before) + 1e-12:
            return False
    cap = limits.get("instrument_fraction")
    if cap is not None:
        for key, after in (post.get("instrument_weights") or {}).items():
            before = baseline.get("instrument_weights", {}).get(key, 0.0)
            if after > max(cap, before) + 1e-12:
                return False
    return True


def finalize_purchase_plan(
    rows: Iterable[dict[str, Any]],
    budget: float | None,
    risk: str,
    *,
    existing_positions: Iterable[dict[str, Any]] = (),
    existing_cash: float = 0.0,
    execution_costs: dict[str, Any] | None = None,
    max_drawdown_loss: float | None = None,
    minimum_cash_reserve_fraction: float = 0.0,
    max_candidate_fraction: float | None = None,
    portfolio_context: str = "use",
    as_of: date | datetime | str | None = None,
    horizon_weeks: int | None = None,
    amount_key: str = "suggested_amount",
    units_key: str = "suggested_units",
    bootstrap_repetitions: int = 128,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reprice and verify only downward changes to approved purchases.

    The baseline for existing breaches includes the purchase budget as unspent
    cash. A new purchase cannot worsen any already breached metric or create a
    new breach. Zero purchases remain possible without claiming unknown or
    already breached holdings are safe. A bounded search retains only verified
    feasible plans; it does not claim a globally optimal discrete solution.
    """
    if risk not in ECONOMIC_SLEEVE_CAPS or portfolio_context not in {"use", "ignore"}:
        raise ValueError("Unsupported portfolio risk policy")
    cash_value = _number(existing_cash)
    if cash_value is None or not 0.0 <= cash_value <= 1e15:
        raise ValueError("Existing cash must be a finite nonnegative amount")
    reserve = _number(minimum_cash_reserve_fraction)
    if reserve is None or not 0.0 <= reserve <= 1.0:
        raise ValueError("Invalid minimum cash reserve")
    if max_drawdown_loss is not None:
        drawdown = _number(max_drawdown_loss)
        if drawdown is None or not 0.0 < drawdown <= 1.0:
            raise ValueError("Maximum drawdown must be greater than zero and at most one")
    if max_candidate_fraction is not None:
        cap = _number(max_candidate_fraction)
        if cap is None or not 0.0 < cap <= 1.0:
            raise ValueError("Invalid maximum instrument fraction")
    if horizon_weeks is not None and (isinstance(horizon_weeks, bool) or not isinstance(horizon_weeks, int) or not 1 <= horizon_weeks <= 5200):
        raise ValueError("Analysis horizon must be an integer from 1 to 5200 weeks")
    contribution = 0.0 if budget is None else _number(budget)
    if contribution is None or not 0.0 <= contribution <= 1e15:
        raise ValueError("Contribution must be a finite nonnegative amount")
    source_rows = [dict(row) for row in rows]
    position_rows = list(existing_positions) if portfolio_context == "use" else []
    cash_value = cash_value if portfolio_context == "use" else 0.0
    values: list[tuple[dict[str, Any], float]] = []
    blockers: list[str] = []
    for position in position_rows:
        quantity = _number(position.get("quantity"))
        if quantity == 0.0:
            continue
        price = _number(position.get("portfolio_price"))
        if quantity is None or quantity < 0.0 or price is None or price <= 0.0:
            blockers.append("existing_position_value_unavailable")
            continue
        if not _candidate_evidence_eligible(position):
            blockers.append("existing_position_evidence_unavailable")
            continue
        value = quantity * price
        if not math.isfinite(value) or value <= 0.0:
            blockers.append("existing_position_value_unavailable")
            continue
        values.append((dict(position), value))

    limits: dict[str, Any] = {
        "annualized_volatility_3y": PORTFOLIO_VOL_TARGET[risk] * RISK_TOLERANCE,
        "expected_shortfall_95_weekly_3y": PORTFOLIO_WEEKLY_ES95_TARGET[risk] * RISK_TOLERANCE,
        "max_drawdown_3y": max_drawdown_loss,
        "sleeves": dict(ECONOMIC_SLEEVE_CAPS[risk]),
        "instrument_fraction": max_candidate_fraction,
        "drawdown_policy": "explicit_user_limit" if max_drawdown_loss is not None else "diagnostic_only",
    }
    pre, _, pre_issues = _snapshot(values, cash_value, as_of=as_of, require_drawdown=max_drawdown_loss is not None)
    baseline, _, baseline_issues = _snapshot(values, cash_value + contribution, as_of=as_of, require_drawdown=max_drawdown_loss is not None)
    blockers.extend(pre_issues)
    blockers.extend(baseline_issues)
    blockers = list(dict.fromkeys(blockers))
    existing_breaches = _breaches(pre, limits) if not blockers else []
    baseline_breaches = _breaches(baseline, limits) if not blockers else []
    prefix = "ai_" if amount_key.startswith("ai_") else ""

    def verify(proposed: list[dict[str, Any]], *, preserve_quantities: bool = False):
        incoming_units = [row.get(units_key) for row in proposed]
        proposed, costs = apply_execution_costs(
            proposed, budget, execution_costs,
            amount_key=amount_key, units_key=units_key,
            reserve_amount=contribution * reserve,
        )
        if preserve_quantities and any(
            Decimal(str(before or 0.0)) != Decimal(str(row.get(units_key) or 0.0))
            for before, row in zip(incoming_units, proposed, strict=True)
        ):
            # Enumerated integers and incumbent fractional quantities are literal
            # choices, not ceilings to shrink proportionally a second time.
            return None
        additions = [(row, float(row.get(amount_key) or 0.0)) for row in proposed if float(row.get(amount_key) or 0.0) > 0.0]
        remaining = costs.get("cash_remaining")
        post, weighted, issues = _snapshot(
            [*values, *additions], cash_value + (contribution if remaining is None else remaining),
            as_of=as_of, require_drawdown=max_drawdown_loss is not None,
        )
        zero = not additions
        permitted = zero or (not blockers and not issues and _non_worsening(post, baseline, limits))
        return proposed, costs, post, weighted, issues, permitted

    def trial(factor: float):
        proposed = [dict(row) for row in source_rows]
        for row in proposed:
            old_amount, old_units = _number(row.get(amount_key)), _number(row.get(units_key))
            valid = _candidate_evidence_eligible(row) and row.get("allocation_eligible") is not False
            if budget is None:
                continue
            row[amount_key] = max(0.0, old_amount or 0.0) * factor if valid else 0.0
            row[units_key] = max(0.0, old_units or 0.0) * factor if valid else 0.0
        return verify(proposed)

    factor = 1.0
    selected = trial(0.0 if blockers else 1.0)
    initial_post, initial_issues = selected[2], selected[4]
    if blockers:
        factor = 0.0
    elif not selected[-1]:
        # A verified zero-purchase result exists even with pre-existing breaches.
        selected = trial(0.0)
        factor, upper = 0.0, 1.0
        # A grid can find a feasible hedge interval that binary search alone
        # misses. Only independently checked plans can become the result.
        for numerator in range(31, 0, -1):
            probe = numerator / 32.0
            candidate = trial(probe)
            if candidate[-1]:
                selected, factor, upper = candidate, probe, (numerator + 1) / 32.0
                break
        for _ in range(24):
            probe = (factor + upper) / 2.0
            candidate = trial(probe)
            if candidate[-1]:
                selected, factor = candidate, probe
            else:
                upper = probe
    purchase_search = search_metadata(selected[1].get("principal", 0.0))
    if budget is not None and (blockers or selected[1].get("status") != "confirmed"):
        purchase_search["status"] = "blocked"
    elif budget is not None:
        search = prepare_whole_search(
            source_rows, selected[0], contribution, contribution * reserve,
            execution_costs, amount_key=amount_key, units_key=units_key,
        )
        purchase_search = search.metadata
        purchase_search["risk_evaluation_limit"] = risk_evaluation_limit(source_rows, position_rows)
        purchase_search["representation_pruned"] = 0
        incumbent_rows = selected[0]
        for state in search.states:
            if state.debit > search.available:
                continue
            if state.score <= search.incumbent.score:
                purchase_search["objective_pruned"] += 1
                continue
            if purchase_search["examined"] >= purchase_search["risk_evaluation_limit"]:
                purchase_search.update(status="bounded", stopped_by_work_limit=True)
                break
            candidate = verify(
                proposed_rows(search, state, source_rows, incumbent_rows,
                              amount_key=amount_key, units_key=units_key),
                preserve_quantities=True,
            )
            if candidate is None:
                purchase_search["representation_pruned"] += 1
                continue
            purchase_search["examined"] += 1
            if candidate[-1]:
                # States are ordered by an exact cash/marked-principal objective.
                # Every remaining state has an equal or worse objective.
                selected = candidate
                purchase_search.update(
                    feasible=purchase_search["feasible"] + 1,
                    selected_source="whole_unit_search", selected_principal=float(state.principal),
                    improvement=True,
                )
                break
            purchase_search["risk_rejected"] += 1
            rejection_issues = [
                *candidate[4],
                *(f"portfolio_limit:{issue}" for issue in _breaches(candidate[2], limits)),
            ] or ["existing_portfolio_breach_would_worsen"]
            purchase_search["risk_rejection_issues"] = list(dict.fromkeys([
                *purchase_search["risk_rejection_issues"], *rejection_issues,
            ]))
        if (purchase_search["full_domain_enumerated"]
                and not purchase_search["stopped_by_work_limit"]
                and not purchase_search["representation_pruned"]):
            purchase_search["optimality_proven"] = True
        if purchase_search["representation_pruned"]:
            purchase_search.update(status="bounded", optimality_proven=False)
        if selected[1].get("principal", 0.0) > 0:
            purchase_search["no_positive_feasible"] = False
        elif purchase_search["optimality_proven"]:
            purchase_search["no_positive_feasible"] = True
    final_rows, costs, post, weighted, issues, permitted = selected
    if not permitted:
        raise AssertionError("Unverified purchase plan escaped the final portfolio gate")
    affordable_but_unverified = (
        costs.get("principal", 0.0) == 0.0 and purchase_search["risk_rejected"] > 0
    )
    if affordable_but_unverified:
        # The proportional incumbent found no affordable purchase, but the
        # integer search demonstrated affordable literal choices and rejected
        # them at the portfolio gate. Keep those two explanations distinct.
        costs["reasons"] = [reason for reason in costs.get("reasons", [])
                            if reason != "execution_costs_no_affordable_purchase"]
        for row in final_rows:
            key = f"{prefix}execution_cost_blockers"
            row[key] = [reason for reason in row.get(key, [])
                        if reason != "execution_costs_no_affordable_purchase"]
    post_breaches = _breaches(post, limits) if not blockers and not issues else []
    reason_list = list(blockers)
    if factor < 1.0 and not blockers:
        reason_list.extend(initial_issues)
        reason_list.extend(f"portfolio_limit:{reason}" for reason in _breaches(initial_post, limits))
        if not reason_list:
            reason_list.append("existing_portfolio_breach_would_worsen")
    reason_list.extend(costs.get("reasons") or [])
    if affordable_but_unverified:
        reason_list.extend(["portfolio_risk_no_verified_purchase",
                            *purchase_search["risk_rejection_issues"]])
    total_principal = costs.get("principal", 0.0)
    if budget is None:
        status = "analysis_only"
    elif blockers or issues:
        status = "unknown"
    elif costs.get("status") != "confirmed":
        status = "blocked"
    elif post_breaches:
        status = "existing_breach_not_worsened"
    else:
        status = "within_limits"
    for before, row in zip(source_rows, final_rows, strict=True):
        if budget is not None:
            for key in (amount_key, units_key):
                ceiling = max(0.0, _number(before.get(key)) or 0.0)
                if float(row.get(key) or 0.0) > ceiling + max(1e-12, ceiling * 1e-14):
                    raise AssertionError("Final portfolio checks increased a purchase")
            if not prefix:
                row["allocation_weight_validated"] = float(row.get(amount_key) or 0.0) / contribution if contribution > 0.0 else 0.0
                row["allocation_eligible"] = float(row.get(amount_key) or 0.0) > 0.0
            elif float(row.get(amount_key) or 0.0) <= 0.0 and row.get("ai_action") == "consider":
                row["ai_action"] = "watch"
        row[f"{prefix}portfolio_risk_blockers"] = list(dict.fromkeys(reason_list))
    evidence = evaluate_risk_evidence(
        weighted, as_of=as_of, max_drawdown_loss=max_drawdown_loss,
        horizon_weeks=horizon_weeks, bootstrap_repetitions=bootstrap_repetitions,
    ) if not blockers else {"status": "unavailable", "issues": blockers}
    portfolio_risk = {
        "contract": CONTRACT_VERSION,
        "scope": "complete_portfolio" if portfolio_context == "use" else "new_contribution_only",
        "status": status,
        "existing_value": None if blockers else math.fsum(value for _, value in values),
        "existing_cash": cash_value,
        "contribution": budget,
        "purchase_budget": budget,
        "cash_after": post.get("cash"),
        "portfolio_value_before": None if blockers else baseline.get("portfolio_value"),
        "portfolio_value_after": None if blockers else post.get("portfolio_value"),
        "pre": pre if not blockers else None,
        "before_purchases": baseline if not blockers else None,
        "baseline_with_contribution": baseline if not blockers else None,
        "post": post if not blockers else None,
        "limits": limits,
        "existing_breaches": existing_breaches,
        "baseline_breaches": baseline_breaches,
        "post_breaches": post_breaches,
        "blockers": list(dict.fromkeys([*reason_list, *issues])),
        "risk_scale": None if purchase_search["improvement"] else factor,
        "proportional_risk_scale": factor,
        "purchase_search": purchase_search,
        "evidence": evidence,
        "assumptions": ["static_current_weights_over_historical_returns", "cash_has_zero_nominal_return", "existing_cash_excludes_purchase_budget", "purchase_budget_may_include_unspent_prior_contributions", "cash_reserve_fraction_applies_to_purchase_budget", "stress_scenarios_are_not_forecasts", "fund_lookthrough_is_not_complete"],
        "drawdown_limit_configured": max_drawdown_loss is not None,
    }
    return final_rows, {
        "budget": budget,
        "deployed": None if budget is None else total_principal,
        "cash_reserve": costs.get("cash_remaining"),
        "deployment_fraction": total_principal / contribution if contribution > 0 else (None if budget is None else 0.0),
        "execution_costs": costs,
        "purchase_search": purchase_search,
        "portfolio_risk": portfolio_risk,
        "final_purchase_contract": CONTRACT_VERSION,
    }
