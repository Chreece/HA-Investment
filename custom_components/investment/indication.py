"""Transparent deterministic investment-indication scoring.

This module is intentionally a ranking/decision-support engine, not a promise of
future returns.  It uses only supplied price history plus current portfolio
exposure and keeps every intermediate metric observable by the UI.

The model combines:
* medium-horizon time-series momentum (roughly 1–12 months),
* moving-average trend confirmation,
* risk-adjusted momentum and trend consistency,
* a short-horizon reversal/overextension guard,
* volatility and drawdown risk,
* portfolio/asset concentration penalties.

Allocation is deliberately conservative: a weak positive signal never forces the
whole cash budget into the market, and volatile candidates receive smaller
weights.  No order execution lives in this module.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import statistics
from typing import Any, Iterable


CATEGORY_VOLATILITY_REFERENCE = {
    "crypto": 0.80,
    "stock": 0.45,
    "etf": 0.30,
    "fund": 0.28,
    "index": 0.25,
    "commodity": 0.40,
    "fx": 0.18,
    "other": 0.45,
}

RISK_PROFILES: dict[str, dict[str, float]] = {
    "very_low": {"vol_ref_mult": 0.60, "risk_penalty_mult": 1.55, "deployment_mult": 0.55, "min_score": 66.0, "min_confidence": 0.65, "regime_floor": -0.15, "allocation_vol_power": 1.35},
    "low": {"vol_ref_mult": 0.78, "risk_penalty_mult": 1.28, "deployment_mult": 0.75, "min_score": 62.0, "min_confidence": 0.55, "regime_floor": -0.30, "allocation_vol_power": 1.15},
    "medium": {"vol_ref_mult": 1.00, "risk_penalty_mult": 1.00, "deployment_mult": 1.00, "min_score": 58.0, "min_confidence": 0.45, "regime_floor": -0.50, "allocation_vol_power": 1.00},
    "high": {"vol_ref_mult": 1.28, "risk_penalty_mult": 0.80, "deployment_mult": 1.00, "min_score": 55.0, "min_confidence": 0.40, "regime_floor": -0.55, "allocation_vol_power": 0.78},
    "very_high": {"vol_ref_mult": 1.60, "risk_penalty_mult": 0.65, "deployment_mult": 1.00, "min_score": 52.0, "min_confidence": 0.35, "regime_floor": -0.60, "allocation_vol_power": 0.62},
}

HORIZON_PROFILES: dict[str, dict[str, Any]] = {
    "very_short": {"momentum": (0.48, 0.34, 0.14, 0.04), "trend": (0.58, 0.32, 0.10), "reversal_mult": 1.55, "regime_mult": 0.55, "risk_mult": 0.80, "history_target": 84.0},
    "short": {"momentum": (0.34, 0.40, 0.22, 0.04), "trend": (0.48, 0.37, 0.15), "reversal_mult": 1.25, "regime_mult": 0.75, "risk_mult": 0.90, "history_target": 126.0},
    "medium": {"momentum": (0.18, 0.34, 0.34, 0.14), "trend": (0.35, 0.35, 0.30), "reversal_mult": 1.00, "regime_mult": 1.00, "risk_mult": 1.00, "history_target": 200.0},
    "long": {"momentum": (0.08, 0.22, 0.36, 0.34), "trend": (0.16, 0.30, 0.54), "reversal_mult": 0.45, "regime_mult": 1.30, "risk_mult": 1.15, "history_target": 504.0},
    "very_long": {"momentum": (0.05, 0.15, 0.30, 0.50), "trend": (0.10, 0.20, 0.70), "reversal_mult": 0.25, "regime_mult": 1.50, "risk_mult": 1.25, "history_target": 756.0},
}

STRATEGY_WEIGHTS: dict[str, dict[str, float]] = {
    "balanced": {"momentum": 20.0, "trend": 8.0, "consistency": 6.0, "risk_adjusted": 6.0, "reversal": 5.0, "rsi": 4.0, "volatility": 8.0, "drawdown": 6.0, "regime": 5.0},
    "momentum": {"momentum": 28.0, "trend": 6.0, "consistency": 7.0, "risk_adjusted": 5.0, "reversal": 4.0, "rsi": 3.0, "volatility": 5.0, "drawdown": 4.0, "regime": 4.0},
    "trend": {"momentum": 14.0, "trend": 16.0, "consistency": 10.0, "risk_adjusted": 5.0, "reversal": 3.0, "rsi": 2.0, "volatility": 6.0, "drawdown": 5.0, "regime": 10.0},
    "risk_adjusted": {"momentum": 14.0, "trend": 7.0, "consistency": 6.0, "risk_adjusted": 12.0, "reversal": 3.0, "rsi": 3.0, "volatility": 13.0, "drawdown": 10.0, "regime": 7.0},
    "pullback": {"momentum": 15.0, "trend": 9.0, "consistency": 6.0, "risk_adjusted": 5.0, "reversal": 12.0, "rsi": 8.0, "volatility": 7.0, "drawdown": 6.0, "regime": 6.0},
}

DIVERSIFICATION_DEFAULT_MAX_FRACTION = {"low": 0.65, "medium": 0.45, "high": 0.30}


def _resolved_strategy(strategy: str, risk_tolerance: str, horizon: str) -> str:
    if strategy != "adaptive":
        return strategy if strategy in STRATEGY_WEIGHTS else "balanced"
    if risk_tolerance in {"very_low", "low"}:
        return "risk_adjusted"
    if horizon in {"long", "very_long"}:
        return "trend"
    if horizon in {"very_short", "short"} and risk_tolerance in {"high", "very_high"}:
        return "momentum"
    return "balanced"


@dataclass(slots=True)
class IndicationResult:
    """One deterministic candidate result."""

    score: float
    label: str
    confidence: float
    price: float
    metrics: dict[str, float | None]
    reasons: list[str]
    warnings: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite_prices(values: Iterable[float]) -> list[float]:
    return [
        float(v)
        for v in values
        if isinstance(v, (int, float))
        and math.isfinite(float(v))
        and float(v) > 0
    ]


def _ret(prices: list[float], periods: int) -> float | None:
    if len(prices) <= periods or prices[-1 - periods] <= 0:
        return None
    return prices[-1] / prices[-1 - periods] - 1.0


def _sma(prices: list[float], periods: int) -> float | None:
    if len(prices) < periods:
        return None
    return sum(prices[-periods:]) / periods


def _rsi(prices: list[float], periods: int = 14) -> float | None:
    if len(prices) <= periods:
        return None
    changes = [prices[i] - prices[i - 1] for i in range(len(prices) - periods, len(prices))]
    gains = sum(max(0.0, c) for c in changes) / periods
    losses = sum(max(0.0, -c) for c in changes) / periods
    if losses <= 1e-15:
        return 100.0 if gains > 0 else 50.0
    rs = gains / losses
    return 100.0 - 100.0 / (1.0 + rs)


def _annualized_volatility(prices: list[float]) -> float | None:
    if len(prices) < 22:
        return None
    returns = [
        prices[i] / prices[i - 1] - 1.0
        for i in range(1, len(prices))
        if prices[i - 1] > 0
    ]
    if len(returns) < 20:
        return None
    return statistics.pstdev(returns[-126:]) * math.sqrt(252.0)


def _max_drawdown(prices: list[float]) -> float | None:
    if not prices:
        return None
    peak = prices[0]
    worst = 0.0
    for price in prices:
        peak = max(peak, price)
        if peak > 0:
            worst = min(worst, price / peak - 1.0)
    return worst


def _current_drawdown(prices: list[float]) -> float | None:
    if not prices:
        return None
    peak = max(prices)
    return prices[-1] / peak - 1.0 if peak > 0 else None


def _trend_consistency(prices: list[float], *, block: int = 21, blocks: int = 6) -> float | None:
    """Return [-1, 1] from recent non-overlapping monthly-ish blocks.

    +1 means every available block rose, -1 every block fell.  This is less
    sensitive than fitting a line to one especially large endpoint move and is
    therefore used as confirmation rather than a primary return forecast.
    """
    available = min(blocks, (len(prices) - 1) // block)
    if available <= 0:
        return None
    signs: list[float] = []
    for offset in range(available, 0, -1):
        end = len(prices) - 1 - (offset - 1) * block
        start = end - block
        if start < 0 or prices[start] <= 0:
            continue
        value = prices[end] / prices[start] - 1.0
        signs.append(1.0 if value > 0 else (-1.0 if value < 0 else 0.0))
    return sum(signs) / len(signs) if signs else None


def _risk_adjusted_momentum(return_126d: float | None, annual_volatility: float | None) -> float | None:
    """Standardize six-month return by six-month volatility.

    This is not advertised as a Sharpe ratio: no risk-free rate is assumed and
    it uses a single historical window.  It is merely a dimensionless momentum
    quality measure used to stop high raw returns/high risk from dominating.
    """
    if return_126d is None or annual_volatility is None or annual_volatility <= 1e-12:
        return None
    six_month_vol = annual_volatility * math.sqrt(126.0 / 252.0)
    return return_126d / six_month_vol if six_month_vol > 1e-12 else None


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _signal(value: float | None, scale: float) -> float:
    """Map an unbounded signed return/ratio to roughly [-1, 1]."""
    if value is None or not math.isfinite(value):
        return 0.0
    return math.tanh(value / scale)


def analyze_prices(
    values: Iterable[float],
    *,
    current_price: float | None = None,
    category: str = "other",
    holding_weight: float = 0.0,
    category_weight: float = 0.0,
    risk_tolerance: str = "medium",
    horizon: str = "medium",
    strategy: str = "adaptive",
    portfolio_overlap: float = 0.0,
    overlap_policy: str = "allow",
) -> IndicationResult:
    """Score one candidate from history, exposure and explicit user intent.

    ``horizon`` is the intended holding period. It changes which momentum and
    trend windows matter instead of pretending the same signal fits a trade held
    for weeks and an investment held for years. ``risk_tolerance`` changes the
    volatility/drawdown calibration and later allocation gates.

    ``portfolio_overlap`` is a best-effort [0, 1] overlap estimate supplied by
    the manager from known ETF/fund constituents. Unknown holdings are never
    silently treated as overlap. Exclusion is handled before this function;
    ``penalize`` reduces the score without inventing diversification.
    """
    if risk_tolerance not in RISK_PROFILES:
        risk_tolerance = "medium"
    if horizon not in HORIZON_PROFILES:
        horizon = "medium"
    if overlap_policy not in {"allow", "penalize", "exclude"}:
        overlap_policy = "allow"
    resolved_strategy = _resolved_strategy(strategy, risk_tolerance, horizon)
    strategy_weights = STRATEGY_WEIGHTS[resolved_strategy]
    risk_profile = RISK_PROFILES[risk_tolerance]
    horizon_profile = HORIZON_PROFILES[horizon]

    prices = _finite_prices(values)
    if current_price is not None and math.isfinite(float(current_price)) and float(current_price) > 0:
        cp = float(current_price)
        if not prices or abs(prices[-1] / cp - 1.0) > 0.002:
            prices.append(cp)
    if not prices:
        raise ValueError("No usable price history")

    price = prices[-1]
    r5, r21, r63, r126, r252, r504, r756 = (_ret(prices, n) for n in (5, 21, 63, 126, 252, 504, 756))
    sma20, sma50, sma200, sma400 = (_sma(prices, n) for n in (20, 50, 200, 400))
    rsi14 = _rsi(prices)
    vol = _annualized_volatility(prices)
    max_dd = _max_drawdown(prices[-252:])
    current_dd = _current_drawdown(prices[-252:])
    consistency = _trend_consistency(prices)
    risk_adjusted = _risk_adjusted_momentum(r126, vol)

    mw = horizon_profile["momentum"]
    momentum = (
        mw[0] * _signal(r21, 0.08)
        + mw[1] * _signal(r63, 0.18)
        + mw[2] * _signal(r126, 0.28)
        + mw[3] * _signal(r252, 0.45)
    )
    # Longer intended holding periods should not be scored from a one-year
    # window alone. When multi-year history exists, progressively blend in
    # two- and three-year momentum; missing long history remains observable in
    # confidence instead of being fabricated.
    if horizon == "long" and r504 is not None:
        momentum = 0.65 * momentum + 0.35 * _signal(r504, 0.80)
    elif horizon == "very_long":
        long_signals = [signal for signal in (_signal(r504, 0.80) if r504 is not None else None, _signal(r756, 1.20) if r756 is not None else None) if signal is not None]
        if long_signals:
            momentum = 0.40 * momentum + 0.60 * (sum(long_signals) / len(long_signals))

    tw = horizon_profile["trend"]
    available_smas = [(sma20, tw[0]), (sma50, tw[1]), (sma200, tw[2])]
    trend_weight = sum(weight for sma, weight in available_smas if sma is not None)
    trend = 0.0
    if trend_weight:
        trend = sum(
            (1.0 if price >= sma else -1.0) * weight
            for sma, weight in available_smas
            if sma is not None
        ) / trend_weight
    if horizon in {"long", "very_long"} and sma400 is not None:
        multi_year_trend = 1.0 if price >= sma400 else -1.0
        trend = (0.75 * trend + 0.25 * multi_year_trend) if trend_weight else multi_year_trend

    reversal = 0.0
    if r5 is not None:
        if r5 > 0.12:
            reversal -= min(1.0, (r5 - 0.12) / 0.18)
        elif r5 < -0.12:
            reversal += (
                min(0.35, (-r5 - 0.12) / 0.30)
                if (r63 or 0) > 0
                else -min(1.0, (-r5 - 0.12) / 0.25)
            )
        elif r5 < 0 and (r63 or 0) > 0:
            reversal += min(0.25, abs(r5) / 0.08)

    rsi_adjust = 0.0
    if rsi14 is not None:
        if rsi14 >= 78:
            rsi_adjust = -0.55
        elif rsi14 >= 70:
            rsi_adjust = -0.25
        elif 45 <= rsi14 <= 62:
            rsi_adjust = 0.15
        elif rsi14 <= 25 and (r63 or 0) < 0:
            rsi_adjust = -0.35

    base_vol_ref = CATEGORY_VOLATILITY_REFERENCE.get(category, CATEGORY_VOLATILITY_REFERENCE["other"])
    vol_ref = base_vol_ref * risk_profile["vol_ref_mult"]
    vol_ratio = (vol / vol_ref) if vol is not None and vol_ref > 0 else None
    vol_adjust = 0.0 if vol_ratio is None else _clip(1.0 - vol_ratio, -1.0, 0.65)

    drawdown_adjust = 0.0
    if current_dd is not None:
        dd = abs(current_dd)
        if dd > 0.45:
            drawdown_adjust = -0.65
        elif dd > 0.30:
            drawdown_adjust = -0.35
        elif 0.05 <= dd <= 0.18 and (r63 or 0) > 0:
            drawdown_adjust = 0.18

    consistency_adjust = 0.0 if consistency is None else _clip(consistency, -1.0, 1.0)
    risk_adjusted_signal = _signal(risk_adjusted, 1.25)

    regime_adjust = 0.0
    if sma200 is not None and r126 is not None:
        if price < sma200 and r126 < 0:
            regime_adjust = -0.55
        elif price >= sma200 and r126 > 0:
            regime_adjust = 0.20

    concentration = _clip(max(0.0, holding_weight - 0.15) / 0.35, 0.0, 1.0)
    category_concentration = _clip(max(0.0, category_weight - 0.45) / 0.40, 0.0, 1.0)
    concentration_penalty = 0.65 * concentration + 0.35 * category_concentration

    risk_component_mult = risk_profile["risk_penalty_mult"] * horizon_profile["risk_mult"]
    overlap = _clip(float(portfolio_overlap or 0.0), 0.0, 1.0)
    # Any known duplication gets a visible minimum penalty, while substantial
    # fund-to-fund overlap scales proportionally. Exact exclusion is a manager
    # policy because an excluded candidate should never reach AI either.
    overlap_score_penalty = 0.0
    if overlap_policy == "penalize" and overlap > 0:
        overlap_score_penalty = 16.0 * max(0.15, overlap)

    raw = (
        50.0
        + strategy_weights["momentum"] * momentum
        + strategy_weights["trend"] * trend
        + strategy_weights["consistency"] * consistency_adjust
        + strategy_weights["risk_adjusted"] * risk_adjusted_signal
        + strategy_weights["reversal"] * horizon_profile["reversal_mult"] * reversal
        + strategy_weights["rsi"] * rsi_adjust
        + strategy_weights["volatility"] * risk_component_mult * vol_adjust
        + strategy_weights["drawdown"] * risk_component_mult * drawdown_adjust
        + strategy_weights["regime"] * horizon_profile["regime_mult"] * regime_adjust
        - 12.0 * concentration_penalty
        - overlap_score_penalty
    )

    data_factor = _clip(len(prices) / float(horizon_profile["history_target"]), 0.20, 1.0)
    confidence_metrics = [r21, r63, r126, sma20, sma50, vol, rsi14, consistency, risk_adjusted]
    if horizon in {"long", "very_long"}:
        confidence_metrics.extend([r504, sma400])
    if horizon == "very_long":
        confidence_metrics.append(r756)
    metric_count = sum(v is not None for v in confidence_metrics)
    confidence = _clip((0.55 * data_factor + 0.45 * metric_count / len(confidence_metrics)), 0.15, 1.0)
    score = 50.0 + (raw - 50.0) * confidence
    score = round(_clip(score, 0.0, 100.0), 2)
    confidence = round(confidence, 4)

    if score >= 75:
        label = "strong_candidate"
    elif score >= 62:
        label = "candidate"
    elif score >= 48:
        label = "watch"
    else:
        label = "caution"

    reasons: list[str] = []
    warnings: list[str] = []
    if (r63 or 0) > 0.05 and (r126 or 0) > 0:
        reasons.append("medium_term_momentum_positive")
    elif (r63 or 0) < -0.05:
        reasons.append("medium_term_momentum_negative")
    if trend > 0.45:
        reasons.append("price_above_trend_averages")
    elif trend < -0.45:
        reasons.append("price_below_trend_averages")
    if consistency is not None and consistency >= 0.5:
        reasons.append("trend_consistency_positive")
    elif consistency is not None and consistency <= -0.5:
        warnings.append("trend_consistency_negative")
    if risk_adjusted is not None and risk_adjusted > 0.75:
        reasons.append("risk_adjusted_momentum_positive")
    elif risk_adjusted is not None and risk_adjusted < -0.75:
        warnings.append("risk_adjusted_momentum_negative")
    if reversal < -0.1:
        warnings.append("short_term_move_may_be_overextended")
    elif reversal > 0.1:
        reasons.append("pullback_inside_stronger_trend")
    if vol_ratio is not None and vol_ratio > 1.35:
        warnings.append("volatility_high_for_selected_risk")
    if current_dd is not None and current_dd < -0.30:
        warnings.append("deep_drawdown")
    if regime_adjust < 0:
        warnings.append("negative_long_trend_regime")
    if concentration_penalty > 0.25:
        warnings.append("portfolio_concentration")
    if overlap > 0:
        warnings.append("portfolio_fund_overlap")
    if confidence < 0.60:
        warnings.append("limited_history")

    return IndicationResult(
        score=score,
        label=label,
        confidence=confidence,
        price=price,
        metrics={
            "return_5d": r5,
            "return_21d": r21,
            "return_63d": r63,
            "return_126d": r126,
            "return_252d": r252,
            "return_504d": r504,
            "return_756d": r756,
            "sma_20": sma20,
            "sma_50": sma50,
            "sma_200": sma200,
            "sma_400": sma400,
            "rsi_14": rsi14,
            "annualized_volatility": vol,
            "volatility_reference": vol_ref,
            "volatility_ratio": vol_ratio,
            "current_drawdown": current_dd,
            "max_drawdown": max_dd,
            "trend_score": trend,
            "trend_consistency": consistency,
            "risk_adjusted_momentum": risk_adjusted,
            "regime_score": regime_adjust,
            "concentration_penalty": concentration_penalty,
            "holding_weight": holding_weight,
            "category_weight": category_weight,
            "portfolio_overlap": overlap,
            "overlap_score_penalty": overlap_score_penalty,
            "risk_tolerance": risk_tolerance,
            "horizon": horizon,
            "strategy": strategy,
            "strategy_resolved": resolved_strategy,
        },
        reasons=reasons,
        warnings=warnings,
    )

def balanced_discovery_sample(
    buckets: dict[str, list[dict[str, Any]]],
    category_order: Iterable[str],
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Round-robin sample a broad discovery pool across asset categories.

    Provider breadth is not itself an investment signal. This helper only
    prevents a large category feed (normally equities) from occupying every
    evaluation slot before the deterministic scorer sees other supported asset
    classes. Ordering inside each provider/category bucket remains stable.
    """
    order = [str(category) for category in category_order if str(category)]
    maximum = max(0, int(limit))
    if maximum <= 0 or not order:
        return []
    selected: list[dict[str, Any]] = []
    index = 0
    while len(selected) < maximum:
        added = False
        for category in order:
            rows = buckets.get(category) or []
            if index < len(rows):
                selected.append(rows[index])
                added = True
                if len(selected) >= maximum:
                    break
        if not added:
            break
        index += 1
    return selected


def attach_relative_strength(results: list[dict[str, Any]]) -> None:
    """Attach a stable candidate-relative percentile without overriding quality.

    Relative rank helps compare several viable candidates, but it never makes a
    weak absolute signal eligible for allocation.  With fewer than three
    candidates there is too little cross-section to call the result meaningful.
    """
    if len(results) < 3:
        for item in results:
            item["relative_strength"] = None
        return
    ordered = sorted(
        enumerate(results), key=lambda pair: (float(pair[1].get("score") or 0.0), pair[0])
    )
    n = len(ordered)
    # Average tied ranks so identical deterministic evidence receives the same
    # percentile instead of depending on provider/input order.
    index = 0
    while index < n:
        score = float(ordered[index][1].get("score") or 0.0)
        end = index + 1
        while end < n and math.isclose(float(ordered[end][1].get("score") or 0.0), score, abs_tol=1e-12):
            end += 1
        avg_rank = (index + end - 1) / 2.0
        percentile = 100.0 * avg_rank / (n - 1) if n > 1 else 50.0
        for pos in range(index, end):
            ordered[pos][1]["relative_strength"] = round(percentile, 2)
        index = end


def _capped_weight_allocations(total: float, weights: list[float], cap_amount: float) -> list[float]:
    """Allocate proportionally while enforcing an absolute per-candidate cap."""
    n = len(weights)
    allocations = [0.0] * n
    active = {i for i, weight in enumerate(weights) if weight > 0}
    remaining = max(0.0, float(total))
    cap = max(0.0, float(cap_amount))
    while active and remaining > 1e-9:
        total_weight = sum(weights[i] for i in active)
        if total_weight <= 0:
            break
        capped_any = False
        for i in list(active):
            share = remaining * weights[i] / total_weight
            room = max(0.0, cap - allocations[i])
            if share >= room - 1e-12:
                allocations[i] += room
                remaining -= room
                active.remove(i)
                capped_any = True
        if capped_any:
            continue
        for i in active:
            allocations[i] += remaining * weights[i] / total_weight
        remaining = 0.0
    return allocations


WHOLE_UNIT_RISK_UPLIFT_FRACTION = {
    "very_low": 0.05,
    "low": 0.10,
    "medium": 0.20,
    "high": 0.30,
    "very_high": 0.40,
}


def _whole_unit_allocations(
    results: list[dict[str, Any]],
    desired: list[float],
    weights: list[float],
    *,
    budget: float,
    target_deployable: float,
    cap_fraction: float,
    reserve_floor: float,
    cap_is_hard: bool,
    risk_tolerance: str,
) -> tuple[list[float], dict[str, Any]]:
    """Convert a desired allocation into deterministic whole-unit lots.

    Automatic diversification caps are soft for the *first* whole lot: a €120
    ETF is still eligible with a €200 budget even when the automatic 45% cap is
    €90.  An explicit user-entered cap remains a hard invariant.  The solver
    adds whole lots in candidate-quality order only while doing so moves the
    portfolio closer to the conviction/risk deployment target and remains
    inside a risk-dependent overshoot ceiling.
    """
    n = len(results)
    allocations = [0.0] * n
    units = [0] * n
    max_total = max(0.0, budget * (1.0 - reserve_floor))
    target = min(max_total, max(0.0, float(target_deployable)))
    uplift = WHOLE_UNIT_RISK_UPLIFT_FRACTION.get(risk_tolerance, WHOLE_UNIT_RISK_UPLIFT_FRACTION["medium"])
    integer_ceiling = min(max_total, target + budget * uplift)
    soft_cap_amount = max(0.0, budget * cap_fraction)

    prices: list[float] = []
    candidate_limits: list[float] = []
    auto_lot_overrides = 0
    for item in results:
        try:
            price = float(item.get("portfolio_price") or item.get("price") or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        if not math.isfinite(price) or price <= 0:
            price = 0.0
        prices.append(price)
        if cap_is_hard:
            limit = soft_cap_amount
        else:
            # Lot-aware automatic cap: permit one whole unit to cross the soft
            # percentage cap, but do not turn that into permission for a second
            # expensive lot.
            limit = max(soft_cap_amount, price) if price > 0 else soft_cap_amount
            if price > soft_cap_amount + 0.005:
                auto_lot_overrides += 1
        candidate_limits.append(min(max_total, limit))

    order = sorted(
        range(n),
        key=lambda i: (
            float(weights[i]) if i < len(weights) else 0.0,
            float(results[i].get("score") or 0.0),
            float(results[i].get("confidence") or 0.0),
            -i,
        ),
        reverse=True,
    )

    current = 0.0
    # At most one loop per purchased unit.  The 10k guard is far above normal
    # ETF/share use while preventing pathological penny-priced data from making
    # an indication request expensive.
    for _ in range(10000):
        current_distance = abs(target - current)
        chosen = None
        for i in order:
            if i >= len(weights) or weights[i] <= 0:
                continue
            price = prices[i]
            if price <= 0:
                continue
            next_candidate = allocations[i] + price
            next_total = current + price
            if next_candidate > candidate_limits[i] + 0.005:
                continue
            if next_total > integer_ceiling + 0.005:
                continue
            # Whole units may cross the fractional target, but only when that
            # lot is a *closer* representation of the target than staying put.
            if abs(target - next_total) >= current_distance - 1e-9:
                continue
            chosen = i
            break
        if chosen is None:
            break
        price = prices[chosen]
        units[chosen] += 1
        allocations[chosen] = round(units[chosen] * price, 2)
        current = round(sum(allocations), 2)

    used_auto_lot_overrides = sum(
        1 for i, allocation in enumerate(allocations)
        if not cap_is_hard and allocation > 0 and prices[i] > soft_cap_amount + 0.005
    )
    return allocations, {
        "whole_unit_target": round(target, 2),
        "whole_unit_integer_ceiling": round(integer_ceiling, 2),
        "whole_unit_soft_candidate_cap": round(soft_cap_amount, 2),
        "whole_unit_cap_is_hard": bool(cap_is_hard),
        "whole_unit_auto_lot_override_candidates": auto_lot_overrides if not cap_is_hard else 0,
        "whole_unit_auto_lot_override_used": used_auto_lot_overrides,
        "whole_unit_adjustment": round(sum(allocations) - target, 2),
    }


def allocate_budget(
    results: list[dict[str, Any]],
    amount: float | None,
    *,
    risk_tolerance: str = "medium",
    diversification: str = "medium",
    min_confidence: float = 0.45,
    max_candidate_fraction: float | None = None,
    minimum_cash_reserve_fraction: float = 0.0,
    whole_units_only: bool = False,
) -> dict[str, float | None]:
    """Attach deterministic allocations under explicit user risk constraints."""
    if risk_tolerance not in RISK_PROFILES:
        risk_tolerance = "medium"
    if diversification not in DIVERSIFICATION_DEFAULT_MAX_FRACTION:
        diversification = "medium"
    profile = RISK_PROFILES[risk_tolerance]
    min_confidence = _clip(float(min_confidence), 0.0, 1.0)
    reserve_floor = _clip(float(minimum_cash_reserve_fraction), 0.0, 1.0)
    cap_is_hard = max_candidate_fraction is not None
    cap_fraction = (
        DIVERSIFICATION_DEFAULT_MAX_FRACTION[diversification]
        if max_candidate_fraction is None
        else _clip(float(max_candidate_fraction), 0.01, 1.0)
    )

    if amount is None:
        for item in results:
            item["suggested_amount"] = None
            item["suggested_units"] = None
        return {"budget": None, "deployed": None, "cash_reserve": None, "deployment_fraction": None, "max_candidate_fraction": cap_fraction, "minimum_cash_reserve_fraction": reserve_floor}

    budget = float(amount)
    if not math.isfinite(budget) or budget < 0:
        raise ValueError("Investment amount must be zero or greater")
    if budget == 0:
        for item in results:
            item["suggested_amount"] = 0.0
            item["suggested_units"] = 0.0
        return {"budget": 0.0, "deployed": 0.0, "cash_reserve": 0.0, "deployment_fraction": 0.0, "max_candidate_fraction": cap_fraction, "minimum_cash_reserve_fraction": reserve_floor}

    # The user-selected confidence floor is the hard floor.  The risk profile's
    # confidence target determines whether the position may receive a normal or
    # only a staged allocation; it must not silently override a visible UI value.
    required_confidence = min_confidence
    full_confidence_target = max(min_confidence, profile["min_confidence"])
    staged_score_floor = max(52.0, profile["min_score"] - 8.0)
    weights: list[float] = []
    qualities: list[float] = []
    for item in results:
        score = float(item.get("score") or 0.0)
        confidence = float(item.get("confidence") or 0.0)
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        regime = float(metrics.get("regime_score") or 0.0)
        vol_ratio_raw = metrics.get("volatility_ratio")
        try:
            vol_ratio = float(vol_ratio_raw) if vol_ratio_raw is not None else 1.0
        except (TypeError, ValueError):
            vol_ratio = 1.0
        if not math.isfinite(vol_ratio) or vol_ratio <= 0:
            vol_ratio = 1.0
        risk_scale = _clip(
            1.0 / (max(0.60, vol_ratio) ** profile["allocation_vol_power"]),
            0.30 if risk_tolerance in {"very_low", "low"} else 0.40,
            1.25,
        )
        relative = item.get("relative_strength")
        relative_scale = 1.0
        if relative is not None:
            try:
                relative_scale = 0.85 + 0.30 * _clip(float(relative) / 100.0, 0.0, 1.0)
            except (TypeError, ValueError):
                relative_scale = 1.0

        regime_ok = regime > profile["regime_floor"]
        full_eligible = (
            score >= profile["min_score"]
            and confidence >= full_confidence_target
            and regime_ok
        )
        staged_eligible = (
            not full_eligible
            and score >= staged_score_floor
            and confidence >= required_confidence
            and regime_ok
        )
        if full_eligible:
            allocation_tier = "full"
            quality = _clip((score - profile["min_score"] + 4.0) / 25.0, 0.0, 1.0) * _clip(confidence, 0.0, 1.0)
            weight = max(0.0, score - (profile["min_score"] - 3.0)) * max(0.0, confidence) * risk_scale * relative_scale
        elif staged_eligible:
            # A near-threshold candidate gets a deliberately small exploratory
            # allocation instead of an all-or-nothing €0 result.  The 0.70 scale
            # keeps low-confidence/low-risk staged entries materially smaller
            # than fully eligible positions and naturally leaves more cash idle.
            allocation_tier = "staged"
            staged_scale = 0.70
            quality = _clip((score - staged_score_floor + 8.0) / 25.0, 0.0, 1.0) * _clip(confidence, 0.0, 1.0) * staged_scale
            weight = max(0.0, score - (staged_score_floor - 2.0)) * max(0.0, confidence) * risk_scale * relative_scale * staged_scale
        else:
            allocation_tier = "none"
            quality = 0.0
            weight = 0.0
        item["allocation_eligible"] = allocation_tier != "none"
        item["allocation_tier"] = allocation_tier
        item["allocation_risk_scale"] = round(risk_scale, 4)
        weights.append(weight)
        qualities.append(quality)

    top_quality = max(qualities, default=0.0)
    strongest_index = max(range(len(qualities)), key=lambda i: (qualities[i], -i), default=None)
    strongest = results[strongest_index] if strongest_index is not None and top_quality > 0 else None
    deployment_fraction = _clip(top_quality * profile["deployment_mult"], 0.0, 1.0 - reserve_floor)
    deployable = round(budget * deployment_fraction, 2)
    cap_amount = round(budget * cap_fraction, 2)
    raw_allocations = _capped_weight_allocations(deployable, weights, cap_amount)
    rounded = [round(value, 2) for value in raw_allocations]
    excess = round(sum(rounded) - deployable, 2)
    if excess > 0 and rounded:
        idx = max(range(len(rounded)), key=lambda i: (rounded[i], -i))
        rounded[idx] = round(max(0.0, rounded[idx] - excess), 2)
    pre_whole_deployed = round(sum(rounded), 2)
    cap_limited = pre_whole_deployed + 0.005 < deployable
    whole_meta: dict[str, Any] = {}

    if whole_units_only:
        rounded, whole_meta = _whole_unit_allocations(
            results,
            rounded,
            weights,
            budget=budget,
            target_deployable=deployable,
            cap_fraction=cap_fraction,
            reserve_floor=reserve_floor,
            cap_is_hard=cap_is_hard,
            risk_tolerance=risk_tolerance,
        )

    for item, suggested in zip(results, rounded, strict=True):
        price = float(item.get("portfolio_price") or item.get("price") or 0.0)
        item["suggested_amount"] = suggested
        if price > 0:
            units = suggested / price
            item["suggested_units"] = float(math.floor(units + 1e-12)) if whole_units_only else round(units, 12)
        else:
            item["suggested_units"] = None
        item["whole_units_only"] = bool(whole_units_only)

    deployed = round(sum(rounded), 2)
    reserve = round(max(0.0, budget - deployed), 2)
    return {
        "budget": round(budget, 2),
        "deployed": deployed,
        "cash_reserve": reserve,
        "deployment_fraction": round(deployed / budget, 4) if budget > 0 else 0.0,
        "target_deployed": deployable,
        "target_deployment_fraction": round(deployment_fraction, 4),
        "pre_whole_deployed": pre_whole_deployed,
        "cap_limited": bool(cap_limited),
        "max_candidate_fraction": cap_fraction,
        "candidate_cap_is_hard": bool(cap_is_hard),
        "minimum_cash_reserve_fraction": reserve_floor,
        "risk_tolerance": risk_tolerance,
        "risk_deployment_multiplier": float(profile["deployment_mult"]),
        "diversification": diversification,
        "required_confidence": round(required_confidence, 4),
        "full_confidence_target": round(full_confidence_target, 4),
        "staged_score_floor": round(staged_score_floor, 2),
        "strongest_quality": round(top_quality, 4),
        "strongest_score": None if strongest is None else round(float(strongest.get("score") or 0.0), 2),
        "strongest_confidence": None if strongest is None else round(float(strongest.get("confidence") or 0.0), 4),
        "strongest_tier": None if strongest is None else strongest.get("allocation_tier"),
        "whole_units_only": bool(whole_units_only),
        **whole_meta,
    }


def sanitize_ai_ranking(
    results: list[dict[str, Any]],
    ranking: Any,
    amount: float | None,
    *,
    max_candidate_fraction: float = 1.0,
    minimum_cash_reserve_fraction: float = 0.0,
    whole_units_only: bool = False,
    max_candidate_fraction_is_hard: bool = True,
    risk_tolerance: str = "medium",
) -> dict[str, float | None]:
    """Apply untrusted AI Task output under deterministic hard limits."""
    valid_actions = {"consider", "watch", "avoid"}
    known = {
        (str(item.get("provider") or ""), str(item.get("provider_id") or "")): item
        for item in results
    }
    proposals: list[tuple[dict[str, Any], float]] = []
    seen: set[tuple[str, str]] = set()
    for item in results:
        item["ai_score"] = None
        item["ai_action"] = "watch"
        item["ai_reason"] = ""
        item["ai_suggested_amount"] = None if amount is None else 0.0
        item["ai_suggested_units"] = None if amount is None else 0.0
    if isinstance(ranking, list):
        for raw in ranking:
            if not isinstance(raw, dict):
                continue
            key = (str(raw.get("provider") or ""), str(raw.get("provider_id") or ""))
            item = known.get(key)
            if item is None or key in seen:
                continue
            seen.add(key)
            try:
                ai_score = float(raw.get("score"))
            except (TypeError, ValueError):
                ai_score = math.nan
            item["ai_score"] = round(_clip(ai_score, 0.0, 100.0), 2) if math.isfinite(ai_score) else None
            action = str(raw.get("action") or "watch").strip().lower()
            if action == "buy":  # compatibility with older AI responses; do not present a direct buy instruction
                action = "consider"
            item["ai_action"] = action if action in valid_actions else "watch"
            item["ai_reason"] = str(raw.get("reason") or "").strip()[:500]
            proposed = 0.0
            if amount is not None and item["ai_action"] == "consider":
                try:
                    parsed = float(raw.get("suggested_amount"))
                    if math.isfinite(parsed) and parsed > 0:
                        proposed = parsed
                except (TypeError, ValueError):
                    pass
            proposals.append((item, proposed))

    if amount is None:
        return {"budget": None, "deployed": None, "cash_reserve": None}

    budget = max(0.0, float(amount))
    reserve_floor = _clip(float(minimum_cash_reserve_fraction), 0.0, 1.0)
    max_deployable = budget * (1.0 - reserve_floor)
    cap_fraction = _clip(float(max_candidate_fraction), 0.01, 1.0)
    cap_amount = budget * cap_fraction
    total = sum(value for _, value in proposals)
    scale = min(1.0, max_deployable / total) if total > 0 else 0.0
    scaled = [round(value * scale, 2) for _, value in proposals]
    if whole_units_only and not max_candidate_fraction_is_hard:
        # Keep the automatic cap soft until lot conversion. The integer solver
        # will enforce the lot-aware soft cap while still respecting the global
        # budget and reserve floor.
        allocations = list(scaled)
    else:
        allocations = [round(min(value, cap_amount), 2) for value in scaled]
    excess = round(sum(allocations) - max_deployable, 2)
    if excess > 0 and allocations:
        idx = max(range(len(allocations)), key=lambda i: (allocations[i], -i))
        allocations[idx] = round(max(0.0, allocations[idx] - excess), 2)
    target_deployed = round(sum(allocations), 2)
    whole_meta: dict[str, Any] = {}
    if whole_units_only:
        proposal_items = [item for item, _ in proposals]
        ai_weights = [
            (max(0.0, float(item.get("ai_score") or 0.0)) + max(0.0, proposed) / max(1.0, budget))
            if proposed > 0 else 0.0
            for item, proposed in proposals
        ]
        allocations, whole_meta = _whole_unit_allocations(
            proposal_items,
            allocations,
            ai_weights,
            budget=budget,
            target_deployable=target_deployed,
            cap_fraction=cap_fraction,
            reserve_floor=reserve_floor,
            cap_is_hard=max_candidate_fraction_is_hard,
            risk_tolerance=risk_tolerance,
        )
    for (item, _), suggested in zip(proposals, allocations, strict=True):
        pp = float(item.get("portfolio_price") or item.get("price") or 0.0)
        item["ai_suggested_amount"] = suggested
        if pp > 0:
            units = suggested / pp
            item["ai_suggested_units"] = float(math.floor(units + 1e-12)) if whole_units_only else round(units, 12)
        else:
            item["ai_suggested_units"] = None
        item["whole_units_only"] = bool(whole_units_only)
    deployed = round(sum(allocations), 2)
    return {
        "budget": round(budget, 2),
        "deployed": deployed,
        "cash_reserve": round(max(0.0, budget - deployed), 2),
        "target_deployed": target_deployed,
        "target_deployment_fraction": round(target_deployed / budget, 4) if budget > 0 else 0.0,
        "max_candidate_fraction": cap_fraction,
        "candidate_cap_is_hard": bool(max_candidate_fraction_is_hard),
        "minimum_cash_reserve_fraction": reserve_floor,
        "whole_units_only": bool(whole_units_only),
        **whole_meta,
    }
