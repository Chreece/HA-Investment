"""Predeclared, deterministic challengers for the contribution-only replay.

These are research policies, not production recommendations.  No parameter is
estimated from investment outcomes.  The 1% cost limit is an explicit experiment
choice, not a threshold established by a paper.  All policies retain the shared
source, identity, suitability, risk, cash and execution checks in the replay.

Scientific motivations (the adaptations below need their own evaluation):
* DeMiguel, Garlappi & Uppal (2009), doi:10.1093/rfs/hhm075: simple benchmarks.
* Faber (2013 update), https://ssrn.com/abstract=962461: monthly 10-month SMA.
  Our rule only filters new purchases; it never sells existing holdings.
* Novy-Marx & Velikov (2016), doi:10.1093/rfs/hhv063: evaluate trading costs.
* Ledoit & Wolf (2004), doi:10.1016/S0047-259X(03)00096-4: scaled-identity
  shrinkage, with the intensity estimated from contemporaneously available data.

Signal ablations zero one raw contribution before confidence scaling/clipping.
They retain all other weights and the original history-reliability calculation.
"""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from datetime import date
from decimal import Decimal, ROUND_CEILING, localcontext
import importlib
import math
import statistics
from typing import Any


STRATEGIES = (
    "baseline_current",
    "cash_only",
    "equal_sleeves",
    "cost_cap_1pct",
    "sma10_equal_sleeves",
    "no_rsi",
    "no_reversal",
    "no_defensive_return_bonus",
    "ledoit_wolf_cap",
)
COST_FRACTION_CAP = Decimal("0.01")
TREND_MONTHS = 10
MONTH_END_MAX_STALE_DAYS = 7
DEFENSIVE_SLEEVES = frozenset({"cash_like", "government_bond", "aggregate_bond"})
_SIMPLE_STRATEGIES = frozenset({"equal_sleeves", "sma10_equal_sleeves"})


def _check_strategy(strategy: str) -> None:
    if strategy not in STRATEGIES:
        raise ValueError(f"Unknown predeclared contribution strategy: {strategy}")


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _decimal(value: Any, name: str) -> Decimal:
    _number(value, name)
    return Decimal(str(value))


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _identity(item: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(item.get("provider") or ""),
        str(item.get("provider_id") or item.get("symbol") or ""),
        str(item.get("symbol") or ""),
    )


def _scorer(model):
    package, _, _ = model.__name__.rpartition(".")
    if not package:
        raise ValueError("Research model must be loaded in its isolated package")
    return importlib.import_module(f"{package}.indication")


def _scaffold(item: Mapping[str, Any]) -> dict[str, Any]:
    original = item.get("research_scaffold_result")
    if isinstance(original, dict):
        return deepcopy(original)
    # Prepared candidates retain the original metrics and scaffold_score.  This
    # fallback is useful for isolated tests, never an inverse of a clipped score.
    result = deepcopy(dict(item))
    result["score"] = item.get("scaffold_score", item.get("score"))
    return result


def _raw_scaffold(
    scaffold: Mapping[str, Any], scorer, neutral_component: str | None = None,
) -> tuple[float, float, dict[str, float]]:
    """Reconstruct the recorded raw formula, including unrounded confidence.

    Every ablation is preceded by a parity check against the recorded baseline.
    A changed production formula therefore fails the experiment instead of
    silently running a different approximation.  Subtracting from the rounded,
    bounded score would be incorrect at 0/100 and suitability boundaries.
    """
    if neutral_component not in {None, "rsi", "reversal"}:
        raise ValueError("Unsupported neutralized signal component")
    metrics = scaffold.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("Signal ablation needs original scaffold metrics")

    def metric(name: str) -> float | None:
        if name not in metrics:
            raise ValueError(f"Missing original scaffold metric: {name}")
        value = metrics[name]
        return None if value is None else _number(value, name)

    horizon = metrics.get("horizon")
    risk = metrics.get("risk_tolerance")
    resolved = metrics.get("strategy_resolved")
    if horizon not in scorer.HORIZON_PROFILES or risk not in scorer.RISK_PROFILES:
        raise ValueError("Unknown original signal policy")
    if resolved not in scorer.STRATEGY_WEIGHTS:
        raise ValueError("Unknown original strategy weights")
    weights = scorer.STRATEGY_WEIGHTS[resolved]
    hp = scorer.HORIZON_PROFILES[horizon]
    rp = scorer.RISK_PROFILES[risk]
    r5, r21, r63, r126, r252, r504, r756, r1260 = (
        metric(f"return_{period}d") for period in (5, 21, 63, 126, 252, 504, 756, 1260)
    )
    sma20, sma50, sma200, sma400 = (metric(f"sma_{period}") for period in (20, 50, 200, 400))
    rsi = metric("rsi_14")
    vol = metric("annualized_volatility")
    consistency = metric("trend_consistency")
    risk_adjusted = metric("risk_adjusted_momentum")
    trend = metric("trend_score")
    regime = metric("regime_score")
    pressure = metric("drawdown_risk_ratio")
    vol_ratio = metric("volatility_ratio")
    concentration = metric("concentration_penalty")
    overlap = metric("overlap_score_penalty")
    if any(value is None for value in (trend, regime, pressure, concentration, overlap)):
        raise ValueError("Incomplete recorded raw-score inputs")

    def signal(value: float | None, scale: float) -> float:
        return 0.0 if value is None else math.tanh(value / scale)

    mw = hp["momentum"]
    momentum = (
        mw[0] * signal(r21, .08) + mw[1] * signal(r63, .18)
        + mw[2] * signal(r126, .28) + mw[3] * signal(r252, .45)
    )
    if horizon == "long" and r504 is not None:
        momentum = .65 * momentum + .35 * signal(r504, .80)
    elif horizon == "very_long":
        long_signals = [signal(value, scale) for value, scale in
                        ((r504, .80), (r756, 1.20), (r1260, 1.80)) if value is not None]
        if long_signals:
            momentum = .35 * momentum + .65 * (sum(long_signals) / len(long_signals))

    reversal = 0.0
    if r5 is not None:
        if r5 > .12:
            reversal -= min(1.0, (r5 - .12) / .18)
        elif r5 < -.12:
            reversal += (min(.35, (-r5 - .12) / .30) if (r63 or 0) > 0
                         else -min(1.0, (-r5 - .12) / .25))
        elif r5 < 0 and (r63 or 0) > 0:
            reversal += min(.25, abs(r5) / .08)
    rsi_adjustment = 0.0
    if rsi is not None:
        if rsi >= 78:
            rsi_adjustment = -.55
        elif rsi >= 70:
            rsi_adjustment = -.25
        elif 45 <= rsi <= 62:
            rsi_adjustment = .15
        elif rsi <= 25 and (r63 or 0) < 0:
            rsi_adjustment = -.35
    contributions = {
        "reversal": weights["reversal"] * float(hp["reversal_mult"]) * reversal,
        "rsi": weights["rsi"] * rsi_adjustment,
    }
    if neutral_component == "reversal":
        reversal = 0.0
    if neutral_component == "rsi":
        rsi_adjustment = 0.0
    risk_mult = float(rp["risk_penalty_mult"]) * float(hp["risk_mult"])
    vol_adjust = 0.0 if vol_ratio is None else _clip(1.0 - vol_ratio, -1.25, .65)
    drawdown_adjust = -_clip((pressure - .45) / 1.20)
    consistency_adjust = 0.0 if consistency is None else _clip(consistency, -1.0, 1.0)
    raw = (
        50.0 + weights["momentum"] * momentum + weights["trend"] * trend
        + weights["consistency"] * consistency_adjust
        + weights["risk_adjusted"] * signal(risk_adjusted, 1.25)
        + weights["reversal"] * float(hp["reversal_mult"]) * reversal
        + weights["rsi"] * rsi_adjustment
        + weights["volatility"] * risk_mult * vol_adjust
        + weights["drawdown"] * risk_mult * drawdown_adjust
        + weights["regime"] * float(hp["regime_mult"]) * regime
        - 12.0 * concentration - overlap
    )
    count = _number(metrics.get("history_observations"), "history_observations")
    if count < 1 or not count.is_integer():
        raise ValueError("History observation count must be a positive integer")
    availability = [r21, r63, r126, sma20, sma50, vol, rsi, consistency, risk_adjusted]
    if horizon in {"long", "very_long"}:
        availability.extend([r504, sma400])
    if horizon == "very_long":
        availability.extend([r756, r1260])
    data_factor = _clip(count / float(hp["history_target"]), .20, 1.0)
    confidence = _clip(.55 * data_factor + .45 * sum(value is not None for value in availability)
                       / max(1, len(availability)), .15, 1.0)
    if round(confidence, 4) != scaffold.get("confidence"):
        raise ValueError("Recorded confidence no longer matches the frozen scaffold formula")
    return raw, confidence, contributions


def _ablate_scaffold(scaffold, scorer, component):
    original_raw, confidence, contributions = _raw_scaffold(scaffold, scorer)
    original_signal = round(_clip(50.0 + (original_raw - 50.0) * confidence, 0.0, 100.0), 2)
    if original_signal != scaffold["metrics"].get("signal_score_before_suitability"):
        raise ValueError("Recorded score no longer matches the frozen raw scaffold formula")
    raw, same_confidence, _ = _raw_scaffold(scaffold, scorer, component)
    signal = round(_clip(50.0 + (raw - 50.0) * same_confidence, 0.0, 100.0), 2)
    out = deepcopy(scaffold)
    out["metrics"]["signal_score_before_suitability"] = signal
    out["score"] = min(signal, 47.0) if out["metrics"].get("suitability_eligible") is False else signal
    out["research_neutralized_component"] = component
    return out, {
        "component": component, "neutral_raw_value": 0.0,
        "removed_raw_contribution": contributions[component],
        "baseline_raw_score": original_raw, "challenger_raw_score": raw,
        "baseline_signal_score": original_signal, "challenger_signal_score": signal,
        "unrounded_data_reliability": confidence,
        "other_weights_rescaled": False,
    }


def _defensive_score_without_bonus(item, scaffold, model):
    sleeve = item.get("economic_sleeve")
    if sleeve not in DEFENSIVE_SLEEVES:
        return item["market_score"], {"applied": False}
    metrics = scaffold.get("metrics") or {}
    if metrics.get("suitability_eligible") is False:
        return item["market_score"], {"applied": False, "reason": "suitability_blocked"}
    confidence = _clip(_number(scaffold.get("confidence"), "confidence"))
    vol = max(0.0, _number(metrics.get("annualized_volatility") or 0.0, "volatility"))
    drawdown = abs(min(0.0, _number(metrics.get("max_drawdown") or 0.0, "max_drawdown")))
    raw_return = metrics.get("return_126d")
    r126 = 0.0 if raw_return is None else _number(raw_return, "return_126d")
    annualized = (1.0 + r126) ** 2 - 1.0 if raw_return is not None and r126 > -1.0 else 0.0
    if sleeve == "cash_like":
        without = (52.0 + 10.0 * _clip(1.0 - vol / .02)
                   + 8.0 * _clip(1.0 - drawdown / .03) + 6.0 * confidence)
        bonus = 14.0 * _clip(annualized / .04)
        # Preserve the production addition order in the baseline parity check.
        before = (52.0 + bonus + 10.0 * _clip(1.0 - vol / .02)
                  + 8.0 * _clip(1.0 - drawdown / .03) + 6.0 * confidence)
        before, without = min(before, 86.0), min(without, 86.0)
    elif sleeve == "government_bond":
        without = (48.0 + 10.0 * _clip(1.0 - vol / .08)
                   + 8.0 * _clip(1.0 - drawdown / .15)
                   + 8.0 * _clip(r126 / .08, -1.0, 1.0) + 6.0 * confidence)
        bonus = 14.0 * _clip(annualized / .05)
        before = (48.0 + bonus + 10.0 * _clip(1.0 - vol / .08)
                  + 8.0 * _clip(1.0 - drawdown / .15)
                  + 8.0 * _clip(r126 / .08, -1.0, 1.0) + 6.0 * confidence)
    else:
        without = (46.0 + 9.0 * _clip(1.0 - vol / .10)
                   + 8.0 * _clip(1.0 - drawdown / .22)
                   + 10.0 * _clip(r126 / .10, -1.0, 1.0) + 6.0 * confidence)
        bonus = 12.0 * _clip(annualized / .06)
        before = (46.0 + bonus + 9.0 * _clip(1.0 - vol / .10)
                  + 8.0 * _clip(1.0 - drawdown / .22)
                  + 10.0 * _clip(r126 / .10, -1.0, 1.0) + 6.0 * confidence)
    baseline = round(_clip(before, 0.0, 100.0), 2)
    if baseline != item["market_score"] or baseline != model.validated_market_score(scaffold, sleeve):
        raise ValueError("Defensive scorer no longer matches the predeclared ablation")
    return round(_clip(without, 0.0, 100.0), 2), {
        "applied": True, "removed_raw_bonus": bonus,
        "annualized_historical_return": annualized,
        "direct_126_day_return_component_preserved": sleeve != "cash_like",
        "replacement_yield_inferred": False,
    }


def _set_market_score(item, scaffold, score, model):
    out = deepcopy(item)
    out.update(market_score=score, score=score)
    confidence = _number(out.get("confidence"), "confidence")
    activation = model.activation(score, confidence)
    if (not model.classify_economic_exposure(out)["allocatable"]
            or not model._candidate_evidence_eligible(out)
            or model._validated_risk_map(out.get("risk_weekly_returns")) is None):
        activation = 0.0
    out["activation"] = activation
    out["allocation_context_scale"] = model.context_allocation_scale(scaffold, score)
    out["label"] = ("strong_candidate" if score >= 75.0 else "candidate" if score >= 62.0
                    else "watch" if score >= model.ACTIVATION_FLOOR else "caution")
    out["metrics"] = dict(out.get("metrics") or {})
    out["metrics"].update(validated_market_score=score, validated_activation=activation,
                          allocation_context_scale=out["allocation_context_scale"])
    return out


def _month_index(day: date) -> int:
    return day.year * 12 + day.month - 1


def _month_end(index: int) -> date:
    year, month0 = divmod(index, 12)
    return date(year, month0 + 1, monthrange(year, month0 + 1)[1])


def completed_month_sma(history: Iterable[Mapping[str, Any]], cutoff: str) -> dict[str, Any]:
    """Use exactly ten consecutive completed calendar months, never partial ones.

    The last available total-return observation represents each month.  A month
    with no observation or one over seven calendar days short of its end is
    unavailable, not replaced by an older month.  This conservative freshness
    check is explicit; it does not claim to reconstruct an exchange calendar.
    Future rows are ignored before their prices are read.
    """
    as_of = date.fromisoformat(cutoff)
    if as_of.isoformat() != cutoff:
        raise ValueError("Trend cutoff must be a canonical ISO date")
    last = _month_index(as_of)
    if as_of < _month_end(last):
        last -= 1
    required = list(range(last - TREND_MONTHS + 1, last + 1))
    by_month: dict[int, tuple[date, float]] = {}
    previous = None
    for row in history:
        observed = date.fromisoformat(row["date"])
        if observed.isoformat() != row["date"]:
            raise ValueError("Trend observations require canonical dates")
        if observed > as_of:
            continue
        if previous is not None and observed <= previous:
            raise ValueError("Trend observations must be unique and chronological")
        previous = observed
        index = _month_index(observed)
        if index not in required:
            continue
        price = _number(row.get("adjusted_close"), "total_return_close")
        if price <= 0:
            raise ValueError("Trend total-return prices must be positive")
        by_month[index] = (observed, price)
    missing = [str(_month_end(index))[:7] for index in required if index not in by_month]
    stale = [str(_month_end(index))[:7] for index in required
             if index in by_month and (_month_end(index) - by_month[index][0]).days > MONTH_END_MAX_STALE_DAYS]
    result = {
        "rule": "last_completed_month_total_return_close_strictly_above_10_month_sma",
        "cutoff": cutoff, "months_required": TREND_MONTHS,
        "latest_completed_month": str(_month_end(last))[:7],
        "missing_months": missing, "stale_months": stale,
        "eligible": False, "close": None, "sma": None,
        "observations": [{"date": by_month[index][0].isoformat(), "adjusted_close": by_month[index][1]}
                         for index in required if index in by_month],
    }
    if missing or stale:
        return result
    prices = [by_month[index][1] for index in required]
    average = statistics.fmean(prices)
    result.update(close=prices[-1], sma=average, eligible=prices[-1] > average)
    return result


def transform_candidates(strategy, candidates, panel, cutoff, model):
    """Return copied candidates and diagnostics; never alter production globals."""
    _check_strategy(strategy)
    output, diagnostics = [], []
    scorer = _scorer(model) if strategy in {"no_rsi", "no_reversal"} else None
    for original in candidates:
        item = deepcopy(original)
        detail: dict[str, Any] = {"symbol": item.get("symbol")}
        if strategy in {"no_rsi", "no_reversal"}:
            component = "rsi" if strategy == "no_rsi" else "reversal"
            changed, detail = _ablate_scaffold(_scaffold(item), scorer, component)
            detail["symbol"] = item.get("symbol")
            prepared = model.prepare_scored_candidate(changed, item, item.get("risk_weekly_returns") or {})
            item.update(prepared)
            item["research_scaffold_result"] = changed
        elif strategy == "no_defensive_return_bonus":
            original_scaffold = _scaffold(item)
            score, detail = _defensive_score_without_bonus(item, original_scaffold, model)
            detail["symbol"] = item.get("symbol")
            item = _set_market_score(item, original_scaffold, score, model)
        if strategy in _SIMPLE_STRATEGIES:
            metrics = item.get("metrics") or {}
            for key in ("holding_weight", "category_weight", "concentration_penalty", "portfolio_overlap", "overlap_score_penalty"):
                if _number(metrics.get(key, 0.0), key) != 0.0:
                    raise ValueError("Simple contribution benchmark received old-portfolio context")
            # context_allocation_scale is zero when the composite signal has no
            # activation even with no context penalty.  That market gate must
            # not inadvertently remain in a signal-independent simple baseline.
            item["allocation_context_scale"] = 1.0
            # Whole-lot projection and minimum-lot recovery also use score and
            # relative-strength tie-breakers.  Preserve the measured composite
            # signal for audit, but remove it from this signal-independent rule.
            item["research_original_market_score"] = item.get("research_original_market_score", item.get("market_score"))
            item.update(market_score=None, score=None, relative_strength=None, activation=None,
                        label="research_control", research_score_semantics="not_used_by_simple_benchmark")
            item["metrics"] = dict(metrics, allocation_context_scale=1.0,
                                   validated_market_score=None, validated_activation=None)
        if strategy == "sma10_equal_sleeves":
            signal = completed_month_sma(panel.history(item["symbol"], cutoff), cutoff)
            item["research_sma10"] = signal
            detail["trend"] = signal
        output.append(item)
        if len(detail) > 1:
            diagnostics.append(detail)
    if strategy in {"no_rsi", "no_reversal", "no_defensive_return_bonus"}:
        # Relative strength is not a new signal here; it is the same existing
        # ranking recalculated after the selected component was neutralized.
        _scorer(model).attach_relative_strength(output)
        output.sort(key=lambda item: (
            float(item.get("score") or 0.0),
            float(item.get("relative_strength") if item.get("relative_strength") is not None else -1.0),
            float(item.get("confidence") or 0.0), str(item.get("symbol") or ""),
        ), reverse=True)
    return output, {"strategy": strategy, "candidate_diagnostics": diagnostics}


def _equal_weights(candidates, risk, model, *, trend: bool):
    if risk not in model.ECONOMIC_SLEEVE_CAPS:
        raise ValueError("Unknown research risk profile")
    groups = defaultdict(list)
    excluded = []
    prepared = []
    for original in candidates:
        item = deepcopy(original)
        classification = model.classify_economic_exposure(item)
        sleeve = classification["economic_sleeve"]
        item.update(economic_sleeve=sleeve, economic_subtype=classification["economic_subtype"],
                    allocatable_economic_exposure=classification["allocatable"])
        eligible = (
            classification["allocatable"] and sleeve in model.SLEEVE_ORDER
            and model._candidate_evidence_eligible(item)
            and model._validated_risk_map(item.get("risk_weekly_returns")) is not None
            and (item.get("metrics") or {}).get("suitability_eligible") is True
        )
        prepared.append(item)
        if not eligible:
            excluded.append(item.get("symbol"))
            continue
        groups[sleeve].append(item)
    active_sleeves = [sleeve for sleeve in model.SLEEVE_ORDER if groups.get(sleeve)
                      and model.ECONOMIC_SLEEVE_CAPS[risk].get(sleeve, 0.0) > 0.0]
    target = 1.0 / len(active_sleeves) if active_sleeves else 0.0
    weighted, targets, omitted = [], {}, []
    for sleeve in active_sleeves:
        sleeve_target = min(target, model.ECONOMIC_SLEEVE_CAPS[risk][sleeve])
        targets[sleeve] = sleeve_target
        per_asset = sleeve_target / len(groups[sleeve])
        for item in sorted(groups[sleeve], key=_identity):
            if trend and (item.get("research_sma10") or {}).get("eligible") is not True:
                omitted.append({"symbol": item.get("symbol"), "unspent_weight": per_asset})
                continue
            weighted.append((item, per_asset))
    weighted, risk_meta = model.scale_down_to_risk_contract(weighted, risk)
    return weighted, {
        "candidates": prepared, "base_sleeve_targets": targets,
        "excluded_for_shared_eligibility": excluded, "omitted_by_trend": omitted,
        "omitted_targets_redistributed": False, "risk": risk_meta,
    }


def ledoit_wolf_covariance(matrix: Sequence[Sequence[float]]) -> tuple[list[list[float]], dict[str, Any]]:
    """Centered, maximum-likelihood covariance shrunk toward mu * identity.

    Implements the Ledoit-Wolf finite-sample intensity estimator without fitting
    any return objective.  With S = X'X/n, its unnormalized numerator is
    ((sum(||x_t||^4)/n) - ||S||_F^2)/n and its denominator is ||S-mu*I||_F^2.
    The resulting ratio is clipped to [0,1].  These are the scaled-identity
    estimator's formulas, not a fixed-intensity or constant-correlation target.
    """
    if not isinstance(matrix, (list, tuple)) or len(matrix) < 2:
        raise ValueError("Shrinkage needs at least two observations")
    first = matrix[0]
    if not isinstance(first, (list, tuple)) or not first:
        raise ValueError("Shrinkage needs a nonempty rectangular matrix")
    n, p = len(matrix), len(first)
    data = []
    for row in matrix:
        if not isinstance(row, (list, tuple)) or len(row) != p:
            raise ValueError("Shrinkage matrix must be rectangular")
        data.append([_number(value, "risk_return") for value in row])
    if any(value < -1.0 for row in data for value in row):
        raise ValueError("Simple asset returns cannot be below minus one")
    means = [math.fsum(row[column] for row in data) / n for column in range(p)]
    centered = [[row[column] - means[column] for column in range(p)] for row in data]
    sample = [[math.fsum(row[i] * row[j] for row in centered) / n
               for j in range(p)] for i in range(p)]
    mu = math.fsum(sample[i][i] for i in range(p)) / p
    delta = math.fsum((sample[i][j] - (mu if i == j else 0.0)) ** 2
                      for i in range(p) for j in range(p))
    norm = math.fsum(value * value for row in sample for value in row)
    fourth = math.fsum(math.fsum(value * value for value in row) ** 2 for row in centered) / n
    beta = max(0.0, (fourth - norm) / n)
    shrinkage = min(1.0, beta / delta) if delta > 0.0 else 0.0
    shrunk = [[(1.0 - shrinkage) * sample[i][j] + (shrinkage * mu if i == j else 0.0)
               for j in range(p)] for i in range(p)]
    if any(not math.isfinite(value) for row in shrunk for value in row):
        raise ValueError("Shrinkage computation overflowed")
    return shrunk, {
        "estimator": "ledoit_wolf_scaled_identity", "observations": n, "assets": p,
        "sample_covariance_divisor": n, "target_mean_variance": mu,
        "shrinkage": shrinkage, "intensity_numerator": beta,
        "intensity_denominator": delta,
    }


def _shrinkage_cap(weighted, risk, model):
    if not weighted:
        return [], {"applied": False, "reason": "no_approved_weights", "scale": 1.0}
    ordered = sorted(weighted, key=lambda pair: _identity(pair[0]))
    maps = [model._validated_risk_map(item.get("risk_weekly_returns")) for item, _ in ordered]
    if any(values is None for values in maps):
        raise ValueError("Shrinkage received invalid risk history")
    dates = sorted(set.intersection(*(set(values) for values in maps)))
    if len(dates) < model.MIN_RISK_HISTORY_WEEKS:
        raise ValueError("Shrinkage received insufficient aligned risk history")
    matrix = [[values[day] for values in maps] for day in dates]
    cov, diagnostics = ledoit_wolf_covariance(matrix)
    weights = [weight for _, weight in ordered]
    variance = math.fsum(weights[i] * cov[i][j] * weights[j]
                         for i in range(len(weights)) for j in range(len(weights)))
    if variance < -1e-14 or not math.isfinite(variance):
        raise ValueError("Invalid shrunk portfolio variance")
    annualized = math.sqrt(max(0.0, variance) * 52.0)
    target = model.PORTFOLIO_VOL_TARGET[risk]
    factor = min(1.0, target / annualized) if annualized > 0.0 else 1.0
    result = [(item, weight * factor) for item, weight in weighted]
    diagnostics.update(applied=True, scale=factor, annualized_volatility=annualized,
                       annualized_volatility_target=target,
                       empirical_risk_gates_retained=True, increased_weights=False,
                       all_positive_weights_scaled_together=True)
    return result, diagnostics


def initial_weights(strategy, candidates, risk, model):
    """Construct only initial targets; the replay still enforces every gate."""
    _check_strategy(strategy)
    if risk not in model.ECONOMIC_SLEEVE_CAPS:
        raise ValueError("Unknown research risk profile")
    if strategy == "cash_only":
        return [], {"strategy": strategy, "candidates": deepcopy(list(candidates)), "cash_fraction": 1.0}
    if strategy in _SIMPLE_STRATEGIES:
        weighted, diagnostics = _equal_weights(candidates, risk, model, trend=strategy == "sma10_equal_sleeves")
    else:
        weighted, diagnostics = model.validated_exact_weights(candidates, risk)
        if strategy == "ledoit_wolf_cap":
            weighted, shrinkage = _shrinkage_cap(weighted, risk, model)
            diagnostics["shrinkage"] = shrinkage
    return weighted, {"strategy": strategy, **diagnostics}


def _cost_profile(costs) -> tuple[Decimal, Decimal]:
    profile = costs.production_profile() if hasattr(costs, "production_profile") else costs
    if not isinstance(profile, Mapping) or profile.get("confirmed") is not True:
        raise ValueError("Cost-cap experiment requires a confirmed cost schedule")
    values = {}
    for key, maximum in (("fixed_fee", 1e9), ("commission_pct", 100), ("spread_bps", 10000), ("fx_bps", 10000)):
        value = _decimal(profile.get(key), key)
        if value < 0 or value > Decimal(str(maximum)):
            raise ValueError(f"Invalid cost schedule: {key}")
        values[key] = value
    return values["fixed_fee"], (values["commission_pct"] / 100
                                 + (values["spread_bps"] + values["fx_bps"]) / 10000)


def filter_approved_rows(strategy, rows, costs):
    """Omit costly approved orders without increasing any other purchase.

    The exact marked principal is units * raw price.  All-in friction includes
    the confirmed charges and the same conservative cent cash rounding as the
    production executor.  The replay must recheck the risk envelope after
    removals, and repeat this filter if another gate subsequently reduces units.
    """
    _check_strategy(strategy)
    output = [deepcopy(row) for row in rows]
    if strategy != "cost_cap_1pct":
        return output, {"strategy": strategy, "applied": False, "removed": []}
    fixed, rate = _cost_profile(costs)
    removed, examined = [], []
    with localcontext() as context:
        context.prec = 60
        for item in output:
            amount = _decimal(item.get("suggested_amount", 0.0), "suggested_amount")
            units = _decimal(item.get("suggested_units", 0.0), "suggested_units")
            if amount < 0 or units < 0:
                raise ValueError("Cost cap received a negative purchase")
            if amount == 0 and units == 0:
                continue
            if amount == 0 or units == 0:
                raise ValueError("Cost cap received inconsistent amount and units")
            raw_price = item.get("portfolio_price", item.get("price"))
            price = _decimal(raw_price, "portfolio_price")
            if price <= 0:
                raise ValueError("Cost cap needs a positive raw unit price")
            principal = units * price
            if principal > amount:
                raise ValueError("Cost cap received units above their monetary authority")
            debit = (principal + fixed + rate * principal).quantize(Decimal(".01"), rounding=ROUND_CEILING)
            friction = debit - principal
            eligible = friction <= COST_FRACTION_CAP * principal
            observation = {
                "symbol": item.get("symbol"), "marked_principal": float(principal),
                "all_in_cost": float(friction), "cash_debit": float(debit),
                "cost_fraction": float(friction / principal), "eligible": eligible,
            }
            examined.append(observation)
            if eligible:
                continue
            removed.append(observation)
            item.update(suggested_amount=0.0, suggested_units=0.0,
                        estimated_transaction_cost=0.0, estimated_cash_debit=0.0,
                        execution_rounding_allowance=0.0, allocation_weight_validated=0.0,
                        allocation_eligible=False, research_cost_cap_blocked=True)
    return output, {
        "strategy": strategy, "applied": True, "cost_fraction_cap": float(COST_FRACTION_CAP),
        "threshold_is_predeclared_research_policy": True,
        "removed": removed, "examined": examined,
        "remaining_orders_increased": False,
        "requires_post_filter_risk_validation": bool(removed),
    }
