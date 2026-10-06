"""Dated portfolio risk diagnostics with explicit, limited interpretations.

All calculations are pure and deterministic. Observed returns are a historical
counterfactual at fixed portfolio weights, with residual cash earning zero.
They are not a forecast, a backtest of executable rebalancing, or a guarantee.
The input boundary expects independently verified positions and source data;
this module validates arithmetic, calendars and declared exposure evidence.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import itertools
import math
from pathlib import Path
import random
import re
import statistics
import sys
from typing import Any, Iterable

if __package__:
    from . import validated_model as _model
else:
    _name = f"{__name__}_validated_model"
    _model = sys.modules.get(_name)
    if _model is None:
        _spec = importlib.util.spec_from_file_location(
            _name, Path(__file__).with_name("validated_model.py")
        )
        _model = importlib.util.module_from_spec(_spec)
        sys.modules[_name] = _model
        _spec.loader.exec_module(_model)

CONTRACT_VERSION = "dated-risk-diagnostics-v1"
MAX_POSITIONS = 256
MAX_HISTORY_WEEKS = 1040
MAX_BOOTSTRAP_REPETITIONS = 256
_WEEK = re.compile(r"([0-9]{4})-W([0-9]{2})\Z")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_BOND_SLEEVES = {"cash_like", "government_bond", "aggregate_bond"}


def _number(value: Any) -> float | None:
    if isinstance(value, (bool, str, bytes)):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _as_of_date(value: Any) -> tuple[dt.date | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None, "as_of_requires_timezone"
        return value.astimezone(dt.timezone.utc).date(), None
    if isinstance(value, dt.date):
        return value, None
    if isinstance(value, str):
        try:
            if _DAY.fullmatch(value):
                return dt.date.fromisoformat(value), None
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return _as_of_date(parsed)
        except ValueError:
            pass
    return None, "invalid_as_of"


def _week_start(value: Any) -> dt.date | None:
    match = _WEEK.fullmatch(value) if isinstance(value, str) else None
    if not match:
        return None
    try:
        return dt.date.fromisocalendar(int(match[1]), int(match[2]), 1)
    except ValueError:
        return None


def _segments(dates: list[dt.date]) -> list[tuple[int, int]]:
    if not dates:
        return []
    boundaries = [0]
    for index in range(1, len(dates)):
        if (dates[index] - dates[index - 1]).days != 7:
            boundaries.append(index)
    boundaries.append(len(dates))
    return list(zip(boundaries[:-1], boundaries[1:]))


def dated_portfolio_returns(
    weighted: Iterable[tuple[dict[str, Any], float]],
    *,
    as_of: Any = None,
) -> dict[str, Any]:
    """Validate and align positive positions without inventing missing weeks.

    ``status='ok'`` requires 52 common observations. Calendar gaps remain an
    explicit issue even then: distribution statistics can use observed weeks,
    whereas a full uninterrupted wealth path cannot bridge those gaps. The
    empty, valid long-only portfolio has the distinct ``cash_only`` status.
    """
    cutoff, cutoff_error = _as_of_date(as_of)
    result: dict[str, Any] = {
        "status": "invalid", "returns": [], "weeks": [], "issues": [],
        "history": {
            "as_of_date": cutoff.isoformat() if cutoff else None,
            "observations": 0, "calendar_gap_count": 0,
            "contiguous_segments": 0, "contiguous_suffix_weeks": 0,
            "first_week": None, "last_week": None,
            "positions": None, "invested_weight": None, "cash_weight": None,
        },
    }
    if cutoff_error:
        result["issues"] = [cutoff_error]
        return result
    maps: list[tuple[float, dict[str, float]]] = []
    try:
        for index, row in enumerate(weighted):
            if index >= MAX_POSITIONS:
                result["issues"] = ["too_many_positions"]
                return result
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                result["issues"] = ["invalid_weighted_position"]
                return result
            item, raw_weight = row
            weight = _number(raw_weight)
            if weight is None or not 0.0 <= weight <= 1.0:
                result["issues"] = ["invalid_position_weight"]
                return result
            if weight == 0.0:
                continue
            if not isinstance(item, dict):
                result["issues"] = ["invalid_position"]
                return result
            raw = item.get("risk_weekly_returns")
            if not isinstance(raw, dict) or not raw:
                result["status"] = "unavailable"
                result["issues"] = ["missing_position_history"]
                return result
            if len(raw) > MAX_HISTORY_WEEKS:
                result["issues"] = ["history_exceeds_bounded_window"]
                return result
            clean: dict[str, float] = {}
            for week, value in raw.items():
                day = _week_start(week)
                number = _number(value)
                if day is None:
                    result["issues"] = ["invalid_iso_week"]
                    return result
                if number is None or number < -1.0:
                    result["issues"] = ["invalid_weekly_return"]
                    return result
                if cutoff and day > cutoff:
                    result["issues"] = ["future_week"]
                    return result
                clean[week] = number
            maps.append((weight, clean))
    except TypeError:
        result["issues"] = ["invalid_weighted_positions"]
        return result
    total = math.fsum(weight for weight, _ in maps)
    if total > 1.0 + 1e-12:
        result["issues"] = ["position_weights_exceed_one"]
        return result
    history = result["history"]
    history.update({
        "positions": len(maps), "invested_weight": total,
        "cash_weight": max(0.0, 1.0 - total),
        "cash_return_assumption": 0.0,
        "source_freshness_verified_here": False,
        "observations": 0, "calendar_gap_count": 0,
        "contiguous_segments": 0, "contiguous_suffix_weeks": 0,
        "first_week": None, "last_week": None,
        "latest_week_may_be_partial": False,
    })
    if not maps:
        result["status"] = "cash_only"
        return result
    weeks = sorted(set.intersection(*(set(raw) for _, raw in maps)))
    try:
        returns = [math.fsum(weight * values[week] for weight, values in maps)
                   for week in weeks]
    except (ValueError, OverflowError):
        result["issues"] = ["nonfinite_portfolio_return"]
        return result
    if any(not math.isfinite(value) or value < -1.0 for value in returns):
        result["issues"] = ["invalid_portfolio_return"]
        return result
    dates = [_week_start(week) for week in weeks]
    segments = _segments(dates)
    history.update({
        "observations": len(weeks),
        "calendar_gap_count": max(0, len(segments) - 1),
        "contiguous_segments": len(segments),
        "contiguous_suffix_weeks": segments[-1][1] - segments[-1][0] if segments else 0,
        "first_week": weeks[0] if weeks else None,
        "last_week": weeks[-1] if weeks else None,
        "latest_week_may_be_partial": bool(dates and cutoff and 0 <= (cutoff - dates[-1]).days < 7),
    })
    result.update({"returns": returns, "weeks": weeks})
    if len(weeks) < _model.MIN_RISK_HISTORY_WEEKS:
        result["status"] = "unavailable"
        result["issues"].append("insufficient_aligned_risk_history")
    else:
        result["status"] = "ok"
    if len(segments) > 1:
        result["issues"].append("calendar_gaps")
    return result


def _tail_description(count: int) -> dict[str, Any]:
    mass = count / 20.0
    complete = int(math.floor(mass))
    return {
        "confidence_level": 0.95, "tail_probability": 0.05,
        "observation_mass": mass, "complete_observations": complete,
        "boundary_observation_fraction": mass - complete,
        "minimum_observations_for_es": 30,
        "interpretation": "empirical_tail_average_not_future_loss_bound",
    }


def _path_metrics(returns: list[float]) -> dict[str, Any]:
    log_value = log_peak = 0.0
    worst = 0.0
    current_underwater = max_underwater = 0
    total_loss = False
    for value in returns:
        if value == -1.0:
            total_loss = True
        if total_loss:
            worst = 1.0
            current_underwater += 1
        else:
            log_value += math.log1p(value)
            log_peak = max(log_peak, log_value)
            worst = max(worst, -math.expm1(log_value - log_peak))
            if log_value < log_peak - 1e-12:
                current_underwater += 1
            else:
                current_underwater = 0
        max_underwater = max(max_underwater, current_underwater)
    try:
        cumulative = -1.0 if total_loss else math.expm1(log_value)
    except OverflowError:
        cumulative = None
    return {
        "max_drawdown_loss": worst,
        "max_observed_underwater_weeks": max_underwater,
        "current_underwater_weeks": current_underwater,
        "unrecovered_at_end": current_underwater > 0,
        "cumulative_return": cumulative,
        "recovery_periods_are_censored_at_sample_end": True,
    }


def _statistics(returns: list[float], *, contiguous: bool) -> dict[str, Any]:
    try:
        vol = statistics.pstdev(returns) * math.sqrt(52.0) if len(returns) >= 2 else None
    except (ValueError, OverflowError):
        vol = None
    if vol is not None and not math.isfinite(vol):
        vol = None
    return {
        "status": "ok" if vol is not None else "unavailable",
        "observations": len(returns),
        "annualized_volatility": vol,
        "expected_shortfall_95_weekly": _model.expected_shortfall_loss(returns),
        "tail": _tail_description(len(returns)),
        "path_status": "observed_contiguous" if contiguous else "unavailable_calendar_gaps",
        "path": _path_metrics(returns) if returns and contiguous else None,
    }


def _quantiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    result: dict[str, float] = {}
    for name, probability in (("lower_05", .05), ("median_50", .5), ("upper_95", .95)):
        location = (len(ordered) - 1) * probability
        low = int(math.floor(location))
        high = int(math.ceil(location))
        fraction = location - low
        result[name] = ordered[low] * (1.0 - fraction) + ordered[high] * fraction
    return result


def _bootstrap(
    returns: list[float], dates: list[dt.date], *, repetitions: int,
    block_weeks: int, seed: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "unavailable", "method": "joint_moving_block_bootstrap",
        "repetitions": repetitions if isinstance(repetitions, int) and not isinstance(repetitions, bool) else None,
        "block_weeks": block_weeks if isinstance(block_weeks, int) and not isinstance(block_weeks, bool) else None,
        "seed": seed if isinstance(seed, int) and not isinstance(seed, bool) else None,
        "source_observations": len(returns),
        "interpretation": "conditional_resampling_quantiles_not_predictive_intervals",
        "assumptions": [
            "stationary_weakly_dependent_source_process",
            "common_asset_indices_preserve_observed_cross_asset_dependence",
            "blocks_never_cross_missing_calendar_weeks",
            "block_length_is_prespecified_not_empirically_calibrated",
            "new_crises_and_future_regime_changes_are_not_generated",
            "resampled_paths_join_blocks_and_are_not_observed_paths",
        ],
    }
    if isinstance(repetitions, bool) or not isinstance(repetitions, int) or not 0 <= repetitions <= MAX_BOOTSTRAP_REPETITIONS:
        result["repetitions"] = None
        result.update({"status": "invalid", "reason": "invalid_bootstrap_repetitions"})
        return result
    if isinstance(block_weeks, bool) or not isinstance(block_weeks, int) or not 2 <= block_weeks <= 52:
        result["block_weeks"] = None
        result.update({"status": "invalid", "reason": "invalid_bootstrap_block_weeks"})
        return result
    if isinstance(seed, bool) or not isinstance(seed, int):
        result["seed"] = None
        result.update({"status": "invalid", "reason": "invalid_bootstrap_seed"})
        return result
    if repetitions == 0:
        result["status"] = "disabled"
        return result
    if repetitions < 32:
        result.update({"status": "invalid", "reason": "bootstrap_requires_at_least_32_repetitions"})
        return result
    if len(returns) < _model.MIN_RISK_HISTORY_WEEKS:
        result["reason"] = "insufficient_aligned_risk_history"
        return result
    starts = [start for left, right in _segments(dates)
              for start in range(left, right - block_weeks + 1)]
    result["valid_source_block_starts"] = len(starts)
    if not starts:
        result["reason"] = "no_contiguous_source_blocks"
        return result
    rng = random.Random(seed)
    distributions: dict[str, list[float]] = {
        "annualized_volatility": [], "expected_shortfall_95_weekly": [],
        "max_drawdown_loss": [],
    }
    for _ in range(repetitions):
        sampled: list[float] = []
        while len(sampled) < len(returns):
            start = starts[rng.randrange(len(starts))]
            sampled.extend(returns[start:start + block_weeks])
        sampled = sampled[:len(returns)]
        metrics = _statistics(sampled, contiguous=True)
        values = {
            "annualized_volatility": metrics["annualized_volatility"],
            "expected_shortfall_95_weekly": metrics["expected_shortfall_95_weekly"],
            "max_drawdown_loss": metrics["path"]["max_drawdown_loss"],
        }
        if any(value is None or not math.isfinite(value) for value in values.values()):
            result["reason"] = "nonfinite_resampled_statistic"
            return result
        for key, value in values.items():
            distributions[key].append(value)
    result["status"] = "ok"
    result["resampling_quantiles"] = {
        key: _quantiles(values) for key, values in distributions.items()
    }
    return result


def _verified_exposure(item: dict[str, Any], field: str, as_of: dt.date | None) -> float | None:
    evidence = item.get("risk_exposures")
    if not isinstance(evidence, dict) or evidence.get("evidence_verified") is not True:
        return None
    raw_date = evidence.get("as_of_date")
    if not isinstance(raw_date, str) or not _DAY.fullmatch(raw_date):
        return None
    try:
        observed_date = dt.date.fromisoformat(raw_date)
    except ValueError:
        return None
    if as_of is None or observed_date > as_of:
        return None
    value = _number(evidence.get(field))
    if value is None or value < 0.0:
        return None
    if field == "unhedged_currency_fraction" and value > 1.0:
        return None
    return value


def _scenarios(
    weighted: list[tuple[dict[str, Any], float]], *, as_of: dt.date | None,
    drawdown_limit: float | None,
) -> dict[str, Any]:
    positions = [(item, weight) for item, weight in weighted if weight > 0.0]
    invested = math.fsum(weight for _, weight in positions)
    shocks = {
        "cash_like": -.01, "government_bond": -.10, "aggregate_bond": -.12,
        "broad_equity": -.30, "sector_equity": -.40, "single_equity": -.45,
        "commodity": -.20, "crypto": -.60,
    }
    known_joint = [(item, weight) for item, weight in positions
                   if item.get("economic_sleeve") in shocks]
    missing_joint = [str(item.get("symbol") or item.get("provider_id") or "unknown")
                     for item, _ in positions if item.get("economic_sleeve") not in shocks]
    joint_loss = -math.fsum(weight * shocks[item["economic_sleeve"]] for item, weight in known_joint)
    rows: list[dict[str, Any]] = [{
        "id": "joint_asset_decline", "status": "unknown" if missing_joint else "modelled",
        "kind": "hypothetical_one_step_shock", "sleeve_returns": shocks,
        "portfolio_loss": None if missing_joint else joint_loss,
        "loss_on_known_positions": joint_loss, "unknown_positions": missing_joint,
        "cash_return": 0.0,
    }]
    # These two generic paths deliberately state their uniform asset movement.
    # Wealth uses unchanged quantities: residual cash is never rebalanced into
    # the falling asset, and a full price recovery restores initial wealth.
    slow_loss = invested * -math.expm1(52 * math.log1p(-.01))
    rows.extend([
        {"id": "sustained_decline", "status": "modelled", "kind": "hypothetical_fixed_quantity_path",
         "weekly_return_each_invested_asset": -.01, "weeks": 52,
         "portfolio_loss": slow_loss, "max_drawdown_loss": slow_loss,
         "assumes_no_trading_or_contributions": True, "cash_return": 0.0},
        {"id": "decline_then_full_rebound", "status": "modelled", "kind": "hypothetical_fixed_quantity_path",
         "returns_each_invested_asset": [-.25, 1.0 / 3.0],
         "portfolio_loss": 0.0, "max_drawdown_loss": invested * .25,
         "assumes_no_trading_or_contributions": True, "cash_return": 0.0},
    ])
    factors = [
        ("rates_up_200bps", "modified_duration", .02, True),
        ("credit_spreads_up_300bps", "credit_spread_duration", .03, True),
        ("unhedged_currency_down_20pct", "unhedged_currency_fraction", .20, False),
    ]
    for scenario_id, field, shock, bonds_only in factors:
        unknown: list[str] = []
        known_loss = 0.0
        applicable_weight = 0.0
        for item, weight in positions:
            sleeve = item.get("economic_sleeve")
            if bonds_only and sleeve in _model.SLEEVE_ORDER and sleeve not in _BOND_SLEEVES:
                continue
            applicable_weight += weight
            value = _verified_exposure(item, field, as_of)
            if value is None or value * shock > 1.0:
                unknown.append(str(item.get("symbol") or item.get("provider_id") or "unknown"))
                continue
            known_loss += weight * value * shock
        rows.append({
            "id": scenario_id, "status": "unknown" if unknown else "modelled",
            "kind": "hypothetical_isolated_factor_sensitivity", "required_exposure": field,
            "shock": shock, "portfolio_loss": None if unknown else known_loss,
            "loss_on_known_positions": known_loss, "unknown_positions": unknown,
            "applicable_position_weight": applicable_weight,
            "assumptions": (["first_order_duration_only", "no_convexity_or_other_factor_response", "non_fixed_income_rate_or_credit_response_unmodelled"]
                            if bonds_only else ["uniform_adverse_move_only_on_verified_unhedged_fraction", "other_price_responses_unmodelled"]),
        })
    reverse: dict[str, Any] = {"status": "not_configured"}
    if drawdown_limit is not None:
        required = drawdown_limit / invested if invested else None
        reverse = {
            "status": "modelled" if required is not None and required <= 1.0 else "unreachable_under_this_scenario",
            "kind": "hypothetical_uniform_one_step_loss", "drawdown_limit": drawdown_limit,
            "each_invested_asset_loss_at_limit": required if required is not None and required <= 1.0 else None,
            "assumptions": ["all_invested_assets_lose_same_fraction", "residual_cash_unchanged", "not_a_future_loss_bound"],
        }
    return {
        "status": "diagnostic_only", "calibrated": False,
        "interpretation": "prespecified_hypothetical_scenarios_not_probabilities_or_allocation_limits",
        "scenarios": rows, "reverse_stress": reverse,
    }


def _horizon_outcomes(
    returns: list[float], dates: list[dt.date], horizon_weeks: Any, goal_return: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "not_configured", "horizon_weeks": horizon_weeks,
        "interpretation": "historical_overlapping_windows_not_forecast_probabilities",
        "assumptions": ["constant_portfolio_weights", "zero_cash_return", "no_new_contributions_or_withdrawals", "no_future_transaction_costs", "nominal_not_inflation_adjusted"],
    }
    if horizon_weeks is None:
        if goal_return is not None:
            result.update({"status": "invalid", "reason": "goal_requires_explicit_horizon"})
        return result
    if isinstance(horizon_weeks, bool) or not isinstance(horizon_weeks, int) or not 1 <= horizon_weeks <= 5200:
        result["horizon_weeks"] = None
        result.update({"status": "invalid", "reason": "invalid_horizon_weeks"})
        return result
    goal = _number(goal_return) if goal_return is not None else None
    if goal_return is not None and (goal is None or goal < -1.0):
        result.update({"status": "invalid", "reason": "invalid_goal_return"})
        return result
    outcomes: list[float] = []
    for left, right in _segments(dates):
        for start in range(left, right - horizon_weeks + 1):
            window = returns[start:start + horizon_weeks]
            value = _path_metrics(window)["cumulative_return"]
            if value is None or not math.isfinite(value):
                result.update({"status": "unavailable", "reason": "nonfinite_horizon_outcome"})
                return result
            outcomes.append(value)
    result["historical_windows"] = len(outcomes)
    if not outcomes:
        result.update({"status": "unavailable", "reason": "insufficient_contiguous_horizon_history"})
        return result
    result.update({
        "status": "observed", "min_horizon_return": min(outcomes),
        "median_horizon_return": statistics.median(outcomes), "max_horizon_return": max(outcomes),
        "negative_return_windows": sum(value < 0.0 for value in outcomes),
        "overlapping_windows_are_not_independent": True,
    })
    if goal is not None:
        failures = sum(value < goal for value in outcomes)
        result.update({"goal_return": goal, "historical_goal_shortfall_count": failures,
                       "historical_goal_shortfall_fraction": failures / len(outcomes)})
    return result


def evaluate_risk_evidence(
    weighted: Iterable[tuple[dict[str, Any], float]],
    *,
    as_of: Any = None,
    max_drawdown_loss: float | None = None,
    horizon_weeks: int | None = None,
    goal_return: float | None = None,
    bootstrap_repetitions: int = 128,
    bootstrap_seed: int = 1729,
    bootstrap_block_weeks: int = 8,
) -> dict[str, Any]:
    """Return reproducible diagnostics, never a success probability or trade.

    ``risk_exposures`` fields are used only when ``evidence_verified is True``
    and an explicit, nonfuture ``as_of_date`` accompanies them. The optional
    fields are ``modified_duration``, ``credit_spread_duration`` and
    ``unhedged_currency_fraction``. Missing metadata stays unknown.
    """
    try:
        positions = list(itertools.islice(weighted, MAX_POSITIONS + 1))
    except TypeError:
        positions = None
    aligned = dated_portfolio_returns(positions, as_of=as_of)
    result: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "status": aligned["status"], "issues": list(aligned["issues"]),
        "history": aligned["history"], "windows": {},
        "historical_return_assumption": "fixed_weights_weekly_counterfactual_not_executable_strategy_backtest",
        "source_freshness_verified_here": False,
        "drawdown_limit": {"status": "not_configured", "limit": None},
    }
    limit = _number(max_drawdown_loss) if max_drawdown_loss is not None else None
    if max_drawdown_loss is not None and (limit is None or not 0.0 <= limit <= 1.0):
        result["status"] = "invalid"
        result["issues"].append("invalid_drawdown_limit")
        return result
    # Validate configuration even for empty/missing histories. An all-cash
    # result must not hide malformed settings or echo NaN into a JSON response.
    horizon_configuration = _horizon_outcomes([], [], horizon_weeks, goal_return)
    if horizon_configuration["status"] == "invalid":
        result["status"] = "invalid"
        result["horizon"] = horizon_configuration
        result["issues"].append(horizon_configuration["reason"])
        return result
    bootstrap_configuration = _bootstrap([], [], repetitions=bootstrap_repetitions,
                                         block_weeks=bootstrap_block_weeks, seed=bootstrap_seed)
    if bootstrap_configuration["status"] == "invalid":
        result["status"] = "invalid"
        result["bootstrap"] = bootstrap_configuration
        result["issues"].append(bootstrap_configuration["reason"])
        return result
    if aligned["status"] == "invalid" or not isinstance(positions, list):
        return result
    if aligned["status"] == "cash_only":
        result["drawdown_limit"] = {"status": "cash_only" if limit is not None else "not_configured", "limit": limit}
        result["bootstrap"] = {"status": "not_applicable_cash_only"}
        result["horizon"] = {"status": "not_applicable_cash_only", "horizon_weeks": horizon_weeks}
        return result
    if not aligned["returns"]:
        result["bootstrap"] = {"status": "unavailable", "reason": "missing_aligned_history"}
        result["horizon"] = {"status": "unavailable", "reason": "missing_aligned_history", "horizon_weeks": horizon_weeks}
        result["stress"] = {"status": "unavailable", "reason": "invalid_or_incomplete_positions"}
        return result
    positions = [(item, _number(weight)) for item, weight in positions if _number(weight) > 0.0]
    returns = aligned["returns"]
    dates = [_week_start(week) for week in aligned["weeks"]]
    suffix = aligned["history"].get("contiguous_suffix_weeks", 0)
    result["tail"] = _tail_description(len(returns))
    result["windows"]["full"] = _statistics(returns, contiguous=aligned["history"]["calendar_gap_count"] == 0)
    for count in (26, 52, 104):
        if suffix < count:
            result["windows"][f"recent_{count}"] = {
                "status": "unavailable", "reason": "insufficient_contiguous_recent_history",
                "required_weeks": count, "available_contiguous_weeks": suffix,
            }
        else:
            result["windows"][f"recent_{count}"] = _statistics(returns[-count:], contiguous=True)
    if limit is not None:
        path = result["windows"]["full"]["path"]
        result["drawdown_limit"] = {
            "status": ("unavailable" if path is None or aligned["status"] != "ok"
                       else "pass" if path["max_drawdown_loss"] <= limit + _model.EPS else "breach"),
            "limit": limit,
            "observed_drawdown_loss": path["max_drawdown_loss"] if path is not None else None,
            "interpretation": "historical_filter_not_guaranteed_future_loss_limit",
        }
    result["bootstrap"] = _bootstrap(returns, dates, repetitions=bootstrap_repetitions,
                                     block_weeks=bootstrap_block_weeks, seed=bootstrap_seed)
    cutoff, _ = _as_of_date(as_of)
    result["stress"] = _scenarios(positions, as_of=cutoff, drawdown_limit=limit)
    result["horizon"] = _horizon_outcomes(returns, dates, horizon_weeks, goal_return)
    return result
