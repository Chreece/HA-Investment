"""Affordable whole-lot recovery for contribution-only Indications.

The validated market score and continuous allocation target do not become more
or less suitable when the user changes the purchase budget.  For instruments
that must be bought in whole units, however, a small budget can leave every
positive continuous target below one unit.  This module permits a single
minimum lot in an otherwise empty economic sleeve when that literal purchase is
still affordable and passes the same evidence, diversification, sleeve and risk
checks as the normal contribution-only plan.

It deliberately does not relax failed evidence, a zero/negative market target,
explicit cash reserves, user candidate limits, execution costs, or the selected
risk envelope.  Existing portfolio holdings are never used by this recovery.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import math
from typing import Any, Callable, Iterable

_EPS = 1e-9


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _whole_required(
    row: dict[str, Any],
    whole_units_only: bool,
    whole_unit_categories: set[str],
) -> bool:
    return bool(
        whole_units_only
        or row.get("whole_units_only")
        or str(row.get("category") or "other").strip().lower()
        in whole_unit_categories
    )


def _priority(row: dict[str, Any], target_weight: float) -> tuple[float, float, float, float, str]:
    return (
        target_weight,
        _number(row.get("market_score") or row.get("score")),
        _number(row.get("confidence")),
        _number(row.get("relative_strength"), -1.0),
        str(row.get("symbol") or row.get("provider_id") or ""),
    )


def build_minimum_lot_trial(
    rows: Iterable[dict[str, Any]],
    *,
    budget: float,
    whole_units_only: bool,
    whole_unit_categories: Iterable[str] | None,
    sleeve_caps: dict[str, float],
    candidate_cap_fraction: float | None,
    reserve_fraction: float,
    evidence_eligible: Callable[[dict[str, Any]], bool],
    risk_acceptable: Callable[[list[dict[str, Any]]], bool],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Add at most one verified minimum lot per otherwise empty sleeve.

    Existing positive allocations are preserved.  A rescue candidate must have
    a positive validated post-constraint weight, require whole units, fit the
    remaining purchase budget, stay inside its economic-sleeve and candidate
    limits, and keep the literal new-contribution portfolio inside the selected
    risk envelope.  Candidates in a sleeve that already has a purchase are not
    enlarged; this is a minimum-lot bridge, not a second allocator.
    """

    output = [deepcopy(row) for row in rows]
    budget = _number(budget)
    if budget <= 0.0:
        return output, {
            "policy": "one_affordable_lot_per_empty_sleeve",
            "eligible_candidates": 0,
            "selected": [],
        }

    categories = {
        str(value).strip().lower()
        for value in (whole_unit_categories or [])
        if str(value).strip()
    }
    reserve = max(0.0, min(1.0, _number(reserve_fraction)))
    available = max(0.0, budget * (1.0 - reserve))
    candidate_cap = (
        1.0
        if candidate_cap_fraction is None
        else max(0.0, min(1.0, _number(candidate_cap_fraction)))
    )

    current_total = 0.0
    sleeve_totals: dict[str, float] = {}
    occupied_sleeves: set[str] = set()
    for row in output:
        amount = max(0.0, _number(row.get("suggested_amount")))
        current_total += amount
        if amount <= _EPS:
            continue
        sleeve = str(row.get("economic_sleeve") or "unknown")
        sleeve_totals[sleeve] = sleeve_totals.get(sleeve, 0.0) + amount
        occupied_sleeves.add(sleeve)

    candidates: list[dict[str, Any]] = []
    for index, row in enumerate(output):
        if max(0.0, _number(row.get("suggested_amount"))) > _EPS:
            continue
        if not _whole_required(row, whole_units_only, categories):
            continue
        target_weight = max(
            0.0,
            _number(
                row.get("allocation_weight_after_constraints"),
                _number(row.get("allocation_weight_exact_validated")),
            ),
        )
        if target_weight <= _EPS:
            continue
        try:
            if not evidence_eligible(row):
                continue
        except Exception:
            continue
        metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
        if metrics.get("suitability_eligible") is False:
            continue
        price = _number(row.get("portfolio_price") or row.get("price"))
        lot_amount = round(price, 2)
        if price <= 0.0 or lot_amount <= 0.0 or lot_amount > available + 0.005:
            continue
        sleeve = str(row.get("economic_sleeve") or "unknown")
        sleeve_cap = max(0.0, min(1.0, _number(sleeve_caps.get(sleeve))))
        if sleeve_cap <= 0.0:
            continue
        candidates.append(
            {
                "index": index,
                "sleeve": sleeve,
                "sleeve_cap": sleeve_cap,
                "lot_amount": lot_amount,
                "target_weight": target_weight,
                "continuous_target": round(target_weight * budget, 2),
                "priority": _priority(row, target_weight),
            }
        )

    candidates.sort(key=lambda item: item["priority"], reverse=True)
    selected: list[dict[str, Any]] = []
    rescued_sleeves: set[str] = set()

    for candidate in candidates:
        sleeve = candidate["sleeve"]
        if sleeve in occupied_sleeves or sleeve in rescued_sleeves:
            continue
        lot_amount = float(candidate["lot_amount"])
        if current_total + lot_amount > available + 0.005:
            continue
        if sleeve_totals.get(sleeve, 0.0) + lot_amount > candidate["sleeve_cap"] * budget + 0.005:
            continue
        if lot_amount > candidate_cap * budget + 0.005:
            continue

        trial = [deepcopy(row) for row in output]
        row = trial[candidate["index"]]
        row["suggested_amount"] = lot_amount
        row["suggested_units"] = 1.0
        row["allocation_weight_validated"] = lot_amount / budget
        row["allocation_eligible"] = True
        row["whole_units_only"] = True
        row["whole_unit_selected"] = True
        row["minimum_lot_override"] = True
        row["minimum_lot_continuous_target"] = candidate["continuous_target"]
        row["minimum_lot_amount"] = lot_amount

        try:
            safe = bool(risk_acceptable(trial))
        except Exception:
            safe = False
        if not safe:
            continue

        output = trial
        current_total += lot_amount
        sleeve_totals[sleeve] = sleeve_totals.get(sleeve, 0.0) + lot_amount
        rescued_sleeves.add(sleeve)
        selected.append(
            {
                "symbol": str(row.get("symbol") or row.get("provider_id") or ""),
                "provider": str(row.get("provider") or ""),
                "provider_id": str(row.get("provider_id") or ""),
                "economic_sleeve": sleeve,
                "continuous_target": candidate["continuous_target"],
                "minimum_lot_amount": lot_amount,
            }
        )

    return output, {
        "policy": "one_affordable_lot_per_empty_sleeve",
        "eligible_candidates": len(candidates),
        "selected": selected,
        "available_after_cash_reserve": round(available, 2),
        "candidate_cap_fraction": candidate_cap,
    }


def recover_affordable_minimum_lots(
    result: dict[str, Any],
    *,
    budget: float | None,
    risk_tolerance: str,
) -> dict[str, Any]:
    """Recover affordable whole-lot suggestions and re-run the final gate."""

    budget_value = _number(budget, -1.0)
    if budget_value <= 0.0 or str(result.get("mode") or "deterministic") == "full_ai":
        return result

    preferences = result.get("preferences") if isinstance(result.get("preferences"), dict) else {}
    whole_units_only = bool(preferences.get("whole_units_only"))
    whole_categories = preferences.get("whole_unit_categories") or []
    if not whole_units_only and not whole_categories:
        return result

    rows = result.get("results") if isinstance(result.get("results"), list) else []
    if not rows:
        return result

    from .portfolio_plan import finalize_purchase_plan
    from .validated_model import (
        ECONOMIC_SLEEVE_CAPS,
        _candidate_evidence_eligible,
        risk_signature,
        within_risk_target,
    )

    allocation = result.get("allocation") if isinstance(result.get("allocation"), dict) else {}
    sleeve_caps = allocation.get("economic_sleeve_caps")
    if not isinstance(sleeve_caps, dict):
        sleeve_caps = dict(ECONOMIC_SLEEVE_CAPS.get(risk_tolerance) or {})
    candidate_cap = _number(
        allocation.get("max_candidate_fraction"),
        _number(preferences.get("effective_max_candidate_pct"), 100.0) / 100.0,
    )
    reserve_fraction = max(
        0.0,
        min(1.0, _number(preferences.get("min_cash_reserve_pct")) / 100.0),
    )

    def risk_acceptable(trial_rows: list[dict[str, Any]]) -> bool:
        weighted = [
            (row, max(0.0, _number(row.get("suggested_amount"))) / budget_value)
            for row in trial_rows
            if max(0.0, _number(row.get("suggested_amount"))) > _EPS
        ]
        return not weighted or within_risk_target(
            risk_signature(weighted), risk_tolerance
        )

    trial_rows, proposal = build_minimum_lot_trial(
        rows,
        budget=budget_value,
        whole_units_only=whole_units_only,
        whole_unit_categories=whole_categories,
        sleeve_caps=sleeve_caps,
        candidate_cap_fraction=candidate_cap,
        reserve_fraction=reserve_fraction,
        evidence_eligible=_candidate_evidence_eligible,
        risk_acceptable=risk_acceptable,
    )
    if not proposal["selected"]:
        return result

    current_deployed = sum(
        max(0.0, _number(row.get("suggested_amount"))) for row in rows
    )
    evidence_as_of = _number(result.get("evidence_as_of"), 0.0)
    as_of = (
        datetime.fromtimestamp(evidence_as_of, UTC).date()
        if evidence_as_of > 0.0
        else None
    )
    max_drawdown_pct = preferences.get("max_drawdown_pct")
    max_drawdown_loss = (
        _number(max_drawdown_pct) / 100.0
        if max_drawdown_pct is not None
        else None
    )
    horizon = preferences.get("analysis_horizon_weeks")
    horizon_weeks = int(horizon) if isinstance(horizon, int) and not isinstance(horizon, bool) else None

    final_rows, final_meta = finalize_purchase_plan(
        trial_rows,
        budget_value,
        risk_tolerance,
        existing_positions=(),
        existing_cash=0.0,
        execution_costs=preferences.get("execution_costs"),
        max_drawdown_loss=max_drawdown_loss,
        minimum_cash_reserve_fraction=reserve_fraction,
        max_candidate_fraction=candidate_cap,
        portfolio_context="ignore",
        as_of=as_of,
        horizon_weeks=horizon_weeks,
        bootstrap_repetitions=0,
    )
    final_deployed = max(0.0, _number(final_meta.get("deployed")))
    if final_deployed <= current_deployed + 0.005:
        return result

    surviving = [
        {
            "symbol": str(row.get("symbol") or row.get("provider_id") or ""),
            "provider": str(row.get("provider") or ""),
            "provider_id": str(row.get("provider_id") or ""),
            "economic_sleeve": str(row.get("economic_sleeve") or "unknown"),
            "continuous_target": _number(row.get("minimum_lot_continuous_target")),
            "minimum_lot_amount": _number(row.get("minimum_lot_amount")),
        }
        for row in final_rows
        if row.get("minimum_lot_override")
        and max(0.0, _number(row.get("suggested_amount"))) > _EPS
    ]
    if not surviving:
        return result

    override_meta = {
        **proposal,
        "used": len(surviving),
        "selected": surviving,
        "deployed_before": round(current_deployed, 2),
        "deployed_after": round(final_deployed, 2),
        "risk_rechecked": True,
        "execution_costs_rechecked": True,
    }

    result["results"] = final_rows
    merged_allocation = result.setdefault("allocation", {})
    projection = merged_allocation.get("projection")
    merged_allocation.update(final_meta)
    if isinstance(projection, dict):
        projection = dict(projection)
        projection["minimum_lot_override"] = override_meta
        projection["post_minimum_lot_deployed"] = round(final_deployed, 2)
        projection["post_minimum_lot_cash_reserve"] = final_meta.get("cash_reserve")
        merged_allocation["projection"] = projection
    merged_allocation["minimum_lot_override"] = override_meta
    merged_allocation["minimum_lot_policy"] = override_meta["policy"]
    result["minimum_lot_override"] = override_meta
    preferences["minimum_lot_policy"] = override_meta["policy"]
    result["preferences"] = preferences
    return result
