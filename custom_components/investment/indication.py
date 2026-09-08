"""Transparent deterministic investment-indication scoring.

The indication engine is a decision-support model, not a promise of future
returns. It separates three concerns that must not be conflated:

* market signal quality (momentum/trend/relative strength),
* suitability for the user's selected risk tolerance, and
* portfolio construction/allocation constraints.

Long-horizon histories are sampled weekly by all supported providers. Short and
medium horizons use daily observations. The scoring windows below therefore map
onto approximately the same elapsed market time across providers instead of
mistaking one weekly observation for one trading day.

No order execution lives in this module.
"""
from __future__ import annotations

from collections import defaultdict
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
    "very_low": {
        "absolute_volatility_target": 0.16,
        "hard_volatility_mult": 1.50,
        "drawdown_target": 0.12,
        "hard_drawdown_limit": 0.40,
        "risk_penalty_mult": 1.60,
        "deployment_mult": 0.55,
        "min_score": 66.0,
        "min_confidence": 0.65,
        "regime_floor": -0.15,
        "allocation_vol_power": 1.40,
    },
    "low": {
        "absolute_volatility_target": 0.22,
        "hard_volatility_mult": 1.70,
        "drawdown_target": 0.20,
        "hard_drawdown_limit": 0.55,
        "risk_penalty_mult": 1.30,
        "deployment_mult": 0.75,
        "min_score": 62.0,
        "min_confidence": 0.55,
        "regime_floor": -0.30,
        "allocation_vol_power": 1.20,
    },
    "medium": {
        "absolute_volatility_target": 0.32,
        "hard_volatility_mult": 2.00,
        "drawdown_target": 0.30,
        "hard_drawdown_limit": 0.70,
        "risk_penalty_mult": 1.00,
        "deployment_mult": 1.00,
        "min_score": 58.0,
        "min_confidence": 0.45,
        "regime_floor": -0.50,
        "allocation_vol_power": 1.00,
    },
    "high": {
        "absolute_volatility_target": 0.45,
        "hard_volatility_mult": 2.30,
        "drawdown_target": 0.42,
        "hard_drawdown_limit": 0.85,
        "risk_penalty_mult": 0.80,
        "deployment_mult": 1.00,
        "min_score": 55.0,
        "min_confidence": 0.40,
        "regime_floor": -0.55,
        "allocation_vol_power": 0.78,
    },
    "very_high": {
        "absolute_volatility_target": 0.60,
        "hard_volatility_mult": 3.00,
        "drawdown_target": 0.55,
        "hard_drawdown_limit": 0.95,
        "risk_penalty_mult": 0.65,
        "deployment_mult": 1.00,
        "min_score": 52.0,
        "min_confidence": 0.35,
        "regime_floor": -0.60,
        "allocation_vol_power": 0.62,
    },
}

HORIZON_PROFILES: dict[str, dict[str, Any]] = {
    "very_short": {"momentum": (0.48, 0.34, 0.14, 0.04), "trend": (0.58, 0.32, 0.10), "reversal_mult": 1.55, "regime_mult": 0.55, "risk_mult": 0.80, "history_target": 84.0, "sampling": "daily"},
    "short": {"momentum": (0.34, 0.40, 0.22, 0.04), "trend": (0.48, 0.37, 0.15), "reversal_mult": 1.25, "regime_mult": 0.75, "risk_mult": 0.90, "history_target": 126.0, "sampling": "daily"},
    "medium": {"momentum": (0.18, 0.34, 0.34, 0.14), "trend": (0.35, 0.35, 0.30), "reversal_mult": 1.00, "regime_mult": 1.00, "risk_mult": 1.00, "history_target": 200.0, "sampling": "daily"},
    "long": {"momentum": (0.08, 0.22, 0.36, 0.34), "trend": (0.16, 0.30, 0.54), "reversal_mult": 0.45, "regime_mult": 1.30, "risk_mult": 1.15, "history_target": 156.0, "sampling": "weekly"},
    "very_long": {"momentum": (0.05, 0.15, 0.30, 0.50), "trend": (0.10, 0.20, 0.70), "reversal_mult": 0.25, "regime_mult": 1.50, "risk_mult": 1.25, "history_target": 260.0, "sampling": "weekly"},
}

STRATEGY_WEIGHTS: dict[str, dict[str, float]] = {
    "balanced": {"momentum": 20.0, "trend": 8.0, "consistency": 6.0, "risk_adjusted": 6.0, "reversal": 5.0, "rsi": 4.0, "volatility": 8.0, "drawdown": 6.0, "regime": 5.0},
    "momentum": {"momentum": 28.0, "trend": 6.0, "consistency": 7.0, "risk_adjusted": 5.0, "reversal": 4.0, "rsi": 3.0, "volatility": 5.0, "drawdown": 4.0, "regime": 4.0},
    "trend": {"momentum": 14.0, "trend": 16.0, "consistency": 10.0, "risk_adjusted": 5.0, "reversal": 3.0, "rsi": 2.0, "volatility": 6.0, "drawdown": 5.0, "regime": 10.0},
    "risk_adjusted": {"momentum": 14.0, "trend": 7.0, "consistency": 6.0, "risk_adjusted": 12.0, "reversal": 3.0, "rsi": 3.0, "volatility": 13.0, "drawdown": 10.0, "regime": 7.0},
    "pullback": {"momentum": 15.0, "trend": 9.0, "consistency": 6.0, "risk_adjusted": 5.0, "reversal": 12.0, "rsi": 8.0, "volatility": 7.0, "drawdown": 6.0, "regime": 6.0},
}

DIVERSIFICATION_DEFAULT_MAX_FRACTION = {"low": 0.65, "medium": 0.45, "high": 0.30}

CATEGORY_MAX_FRACTION_BY_RISK: dict[str, dict[str, float]] = {
    "very_low": {"crypto": 0.00, "stock": 0.20, "etf": 0.60, "fund": 0.60, "index": 0.30, "commodity": 0.05, "fx": 0.10, "other": 0.10},
    "low": {"crypto": 0.03, "stock": 0.30, "etf": 0.70, "fund": 0.70, "index": 0.40, "commodity": 0.10, "fx": 0.15, "other": 0.15},
    "medium": {"crypto": 0.10, "stock": 0.45, "etf": 0.80, "fund": 0.80, "index": 0.50, "commodity": 0.15, "fx": 0.20, "other": 0.25},
    "high": {"crypto": 0.25, "stock": 0.65, "etf": 0.90, "fund": 0.90, "index": 0.70, "commodity": 0.25, "fx": 0.30, "other": 0.45},
    "very_high": {"crypto": 0.40, "stock": 0.85, "etf": 1.00, "fund": 1.00, "index": 0.85, "commodity": 0.40, "fx": 0.40, "other": 0.70},
}

INDICATION_METHOD = "risk_horizon_suitability_allocation_v7"
DISCOVERY_CATEGORY_WEIGHTS = {"etf": 4, "fund": 3, "stock": 3, "index": 2, "commodity": 2, "fx": 1, "crypto": 1, "other": 1}

EU_DISCOVERY_REGIONS = frozenset({"germany", "eu_eea"})
EU_LISTING_SUFFIXES = (".DE", ".AS", ".PA", ".MI", ".L", ".SW", ".VI", ".BR", ".MC", ".IR", ".ST", ".CO", ".HE", ".OL")


def candidate_region_compatible(item: dict[str, Any], legal_region: str | None) -> bool:
    """Return whether a broad-discovery row is compatible with the selected region."""
    region = str(legal_region or "").strip().lower()
    if region not in EU_DISCOVERY_REGIONS:
        return True
    category = str(item.get("category") or "other").strip().lower()
    symbol = str(item.get("provider_id") or item.get("symbol") or "").strip().upper()
    name = str(item.get("name") or "").strip().upper()
    european_listing = any(symbol.endswith(suffix) for suffix in EU_LISTING_SUFFIXES)
    if category == "fund":
        return european_listing
    if category == "etf":
        return european_listing or "UCITS" in name
    return True


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
    score: float
    label: str
    confidence: float
    price: float
    metrics: dict[str, Any]
    reasons: list[str]
    warnings: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite_prices(values: Iterable[float]) -> list[float]:
    return [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(float(v)) and float(v) > 0]


def _ret(prices: list[float], periods: int) -> float | None:
    if periods <= 0 or len(prices) <= periods or prices[-1 - periods] <= 0:
        return None
    return prices[-1] / prices[-1 - periods] - 1.0


def _sma(prices: list[float], periods: int) -> float | None:
    if periods <= 0 or len(prices) < periods:
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


def _annualized_volatility(prices: list[float], *, periods_per_year: float, lookback_periods: int) -> float | None:
    if len(prices) < 8:
        return None
    returns = [prices[i] / prices[i - 1] - 1.0 for i in range(1, len(prices)) if prices[i - 1] > 0]
    minimum = 12 if periods_per_year <= 60 else 20
    if len(returns) < minimum:
        return None
    tail = returns[-max(minimum, int(lookback_periods)):]
    if len(tail) < minimum:
        return None
    return statistics.pstdev(tail) * math.sqrt(periods_per_year)


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


def _trend_consistency(prices: list[float], *, block: int, blocks: int = 6) -> float | None:
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


def _risk_adjusted_momentum(half_year_return: float | None, annual_volatility: float | None) -> float | None:
    if half_year_return is None or annual_volatility is None or annual_volatility <= 1e-12:
        return None
    half_year_vol = annual_volatility * math.sqrt(0.5)
    return half_year_return / half_year_vol if half_year_vol > 1e-12 else None


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _signal(value: float | None, scale: float) -> float:
    if value is None or not math.isfinite(value):
        return 0.0
    return math.tanh(value / scale)


def _sampling_windows(horizon: str) -> dict[str, int | float | str]:
    if horizon in {"long", "very_long"}:
        return {"sampling": "weekly", "periods_per_year": 52.0, "r5": 1, "r21": 4, "r63": 13, "r126": 26, "r252": 52, "r504": 104, "r756": 156, "r1260": 260, "sma20": 4, "sma50": 10, "sma200": 40, "sma400": 80, "rsi": 14, "consistency_block": 4, "vol_window": 52}
    return {"sampling": "daily", "periods_per_year": 252.0, "r5": 5, "r21": 21, "r63": 63, "r126": 126, "r252": 252, "r504": 504, "r756": 756, "r1260": 1260, "sma20": 20, "sma50": 50, "sma200": 200, "sma400": 400, "rsi": 14, "consistency_block": 21, "vol_window": 126}


def _category_cap_fraction(risk_tolerance: str, category: str) -> float:
    profile = CATEGORY_MAX_FRACTION_BY_RISK.get(risk_tolerance, CATEGORY_MAX_FRACTION_BY_RISK["medium"])
    return _clip(float(profile.get(category, profile.get("other", 0.25))), 0.0, 1.0)


def _assess_suitability(*, category: str, risk_tolerance: str, annualized_volatility: float | None, max_drawdown: float | None) -> dict[str, Any]:
    profile = RISK_PROFILES[risk_tolerance]
    category_cap = _category_cap_fraction(risk_tolerance, category)
    hard_vol_limit = float(profile["absolute_volatility_target"]) * float(profile["hard_volatility_mult"])
    hard_drawdown_limit = float(profile["hard_drawdown_limit"])
    eligible = category_cap > 0.0
    blockers: list[str] = []
    if category_cap <= 0.0:
        blockers.append("asset_class_not_suitable_for_selected_risk")
    if annualized_volatility is not None and math.isfinite(annualized_volatility) and annualized_volatility > hard_vol_limit:
        eligible = False
        blockers.append("absolute_volatility_above_risk_limit")
    if max_drawdown is not None and math.isfinite(max_drawdown) and abs(min(0.0, max_drawdown)) > hard_drawdown_limit:
        eligible = False
        blockers.append("historical_drawdown_above_risk_limit")
    return {"eligible": eligible, "blockers": blockers, "category_cap_fraction": category_cap, "absolute_volatility_limit": hard_vol_limit, "absolute_drawdown_limit": hard_drawdown_limit}


def analyze_prices(values: Iterable[float], *, current_price: float | None = None, category: str = "other", holding_weight: float = 0.0, category_weight: float = 0.0, risk_tolerance: str = "medium", horizon: str = "medium", strategy: str = "adaptive", portfolio_overlap: float = 0.0, overlap_policy: str = "allow") -> IndicationResult:
    if risk_tolerance not in RISK_PROFILES:
        risk_tolerance = "medium"
    if horizon not in HORIZON_PROFILES:
        horizon = "medium"
    if overlap_policy not in {"allow", "penalize", "exclude"}:
        overlap_policy = "allow"
    category = str(category or "other")
    resolved_strategy = _resolved_strategy(strategy, risk_tolerance, horizon)
    strategy_weights = STRATEGY_WEIGHTS[resolved_strategy]
    risk_profile = RISK_PROFILES[risk_tolerance]
    horizon_profile = HORIZON_PROFILES[horizon]
    windows = _sampling_windows(horizon)

    prices = _finite_prices(values)
    if current_price is not None and math.isfinite(float(current_price)) and float(current_price) > 0:
        cp = float(current_price)
        if not prices:
            prices.append(cp)
        elif abs(prices[-1] / cp - 1.0) > 0.002:
            if windows["sampling"] == "weekly":
                prices[-1] = cp
            else:
                prices.append(cp)
    if not prices:
        raise ValueError("No usable price history")

    price = prices[-1]
    r5 = _ret(prices, int(windows["r5"]))
    r21 = _ret(prices, int(windows["r21"]))
    r63 = _ret(prices, int(windows["r63"]))
    r126 = _ret(prices, int(windows["r126"]))
    r252 = _ret(prices, int(windows["r252"]))
    r504 = _ret(prices, int(windows["r504"]))
    r756 = _ret(prices, int(windows["r756"]))
    r1260 = _ret(prices, int(windows["r1260"]))
    sma20 = _sma(prices, int(windows["sma20"]))
    sma50 = _sma(prices, int(windows["sma50"]))
    sma200 = _sma(prices, int(windows["sma200"]))
    sma400 = _sma(prices, int(windows["sma400"]))
    rsi14 = _rsi(prices, int(windows["rsi"]))
    vol = _annualized_volatility(prices, periods_per_year=float(windows["periods_per_year"]), lookback_periods=int(windows["vol_window"]))
    max_dd = _max_drawdown(prices)
    current_dd = _current_drawdown(prices)
    consistency = _trend_consistency(prices, block=int(windows["consistency_block"]))
    risk_adjusted = _risk_adjusted_momentum(r126, vol)

    mw = horizon_profile["momentum"]
    momentum = mw[0] * _signal(r21, 0.08) + mw[1] * _signal(r63, 0.18) + mw[2] * _signal(r126, 0.28) + mw[3] * _signal(r252, 0.45)
    if horizon == "long" and r504 is not None:
        momentum = 0.65 * momentum + 0.35 * _signal(r504, 0.80)
    elif horizon == "very_long":
        long_signals = [signal for signal in (_signal(r504, 0.80) if r504 is not None else None, _signal(r756, 1.20) if r756 is not None else None, _signal(r1260, 1.80) if r1260 is not None else None) if signal is not None]
        if long_signals:
            momentum = 0.35 * momentum + 0.65 * (sum(long_signals) / len(long_signals))

    tw = horizon_profile["trend"]
    available_smas = [(sma20, tw[0]), (sma50, tw[1]), (sma200, tw[2])]
    trend_weight = sum(weight for sma, weight in available_smas if sma is not None)
    trend = 0.0
    if trend_weight:
        trend = sum((1.0 if price >= sma else -1.0) * weight for sma, weight in available_smas if sma is not None) / trend_weight
    if horizon in {"long", "very_long"} and sma400 is not None:
        multi_year_trend = 1.0 if price >= sma400 else -1.0
        trend = 0.75 * trend + 0.25 * multi_year_trend if trend_weight else multi_year_trend

    reversal = 0.0
    if r5 is not None:
        if r5 > 0.12:
            reversal -= min(1.0, (r5 - 0.12) / 0.18)
        elif r5 < -0.12:
            reversal += min(0.35, (-r5 - 0.12) / 0.30) if (r63 or 0) > 0 else -min(1.0, (-r5 - 0.12) / 0.25)
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

    absolute_vol_ref = float(risk_profile["absolute_volatility_target"])
    vol_ratio = vol / absolute_vol_ref if vol is not None and absolute_vol_ref > 0 else None
    vol_adjust = 0.0 if vol_ratio is None else _clip(1.0 - vol_ratio, -1.25, 0.65)
    category_vol_ref = CATEGORY_VOLATILITY_REFERENCE.get(category, CATEGORY_VOLATILITY_REFERENCE["other"])
    category_vol_ratio = vol / category_vol_ref if vol is not None and category_vol_ref > 0 else None

    drawdown_target = float(risk_profile["drawdown_target"])
    current_dd_ratio = abs(min(0.0, current_dd)) / drawdown_target if current_dd is not None and drawdown_target > 0 else 0.0
    max_dd_reference = max(0.05, drawdown_target * 1.80)
    max_dd_ratio = abs(min(0.0, max_dd)) / max_dd_reference if max_dd is not None else 0.0
    drawdown_pressure = max(current_dd_ratio, max_dd_ratio)
    drawdown_adjust = -_clip((drawdown_pressure - 0.45) / 1.20, 0.0, 1.0)

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
    risk_component_mult = float(risk_profile["risk_penalty_mult"]) * float(horizon_profile["risk_mult"])
    overlap = _clip(float(portfolio_overlap or 0.0), 0.0, 1.0)
    overlap_score_penalty = 16.0 * max(0.15, overlap) if overlap_policy == "penalize" and overlap > 0 else 0.0

    raw = 50.0 + strategy_weights["momentum"] * momentum + strategy_weights["trend"] * trend + strategy_weights["consistency"] * consistency_adjust + strategy_weights["risk_adjusted"] * risk_adjusted_signal + strategy_weights["reversal"] * float(horizon_profile["reversal_mult"]) * reversal + strategy_weights["rsi"] * rsi_adjust + strategy_weights["volatility"] * risk_component_mult * vol_adjust + strategy_weights["drawdown"] * risk_component_mult * drawdown_adjust + strategy_weights["regime"] * float(horizon_profile["regime_mult"]) * regime_adjust - 12.0 * concentration_penalty - overlap_score_penalty

    data_factor = _clip(len(prices) / float(horizon_profile["history_target"]), 0.20, 1.0)
    confidence_metrics: list[float | None] = [r21, r63, r126, sma20, sma50, vol, rsi14, consistency, risk_adjusted]
    if horizon in {"long", "very_long"}:
        confidence_metrics.extend([r504, sma400])
    if horizon == "very_long":
        confidence_metrics.extend([r756, r1260])
    metric_count = sum(v is not None for v in confidence_metrics)
    confidence = _clip(0.55 * data_factor + 0.45 * metric_count / max(1, len(confidence_metrics)), 0.15, 1.0)
    signal_score = round(_clip(50.0 + (raw - 50.0) * confidence, 0.0, 100.0), 2)
    confidence = round(confidence, 4)

    suitability = _assess_suitability(category=category, risk_tolerance=risk_tolerance, annualized_volatility=vol, max_drawdown=max_dd)
    score = min(signal_score, 47.0) if not suitability["eligible"] else signal_score
    if not suitability["eligible"]:
        label = "caution"
    elif score >= 75:
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
    if vol_ratio is not None and vol_ratio > 1.0:
        warnings.append("volatility_high_for_selected_risk")
    if drawdown_pressure > 1.0:
        warnings.append("drawdown_high_for_selected_risk")
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
        score=round(score, 2),
        label=label,
        confidence=confidence,
        price=price,
        metrics={"return_5d": r5, "return_21d": r21, "return_63d": r63, "return_126d": r126, "return_252d": r252, "return_504d": r504, "return_756d": r756, "return_1260d": r1260, "sma_20": sma20, "sma_50": sma50, "sma_200": sma200, "sma_400": sma400, "rsi_14": rsi14, "annualized_volatility": vol, "volatility_reference": absolute_vol_ref, "volatility_ratio": vol_ratio, "category_volatility_reference": category_vol_ref, "category_volatility_ratio": category_vol_ratio, "current_drawdown": current_dd, "max_drawdown": max_dd, "drawdown_risk_ratio": drawdown_pressure, "trend_score": trend, "trend_consistency": consistency, "risk_adjusted_momentum": risk_adjusted, "regime_score": regime_adjust, "concentration_penalty": concentration_penalty, "holding_weight": holding_weight, "category_weight": category_weight, "portfolio_overlap": overlap, "overlap_score_penalty": overlap_score_penalty, "risk_tolerance": risk_tolerance, "horizon": horizon, "strategy": strategy, "strategy_resolved": resolved_strategy, "sampling": windows["sampling"], "history_observations": len(prices), "history_target_observations": horizon_profile["history_target"], "confidence_semantics": "historical_data_reliability", "signal_score_before_suitability": signal_score, "suitability_eligible": bool(suitability["eligible"]), "suitability_blockers": list(suitability["blockers"]), "category_cap_fraction": float(suitability["category_cap_fraction"]), "absolute_volatility_limit": float(suitability["absolute_volatility_limit"]), "absolute_drawdown_limit": float(suitability["absolute_drawdown_limit"])},
        reasons=reasons,
        warnings=warnings,
    )


def balanced_discovery_sample(buckets: dict[str, list[dict[str, Any]]], category_order: Iterable[str], *, limit: int = 20) -> list[dict[str, Any]]:
    order = [str(category) for category in category_order if str(category)]
    maximum = max(0, int(limit))
    if maximum <= 0 or not order:
        return []
    selected: list[dict[str, Any]] = []
    next_index = {category: 0 for category in order}
    for category in order:
        rows = buckets.get(category) or []
        if rows and len(selected) < maximum:
            selected.append(rows[0])
            next_index[category] = 1
    if len(selected) >= maximum:
        return selected[:maximum]
    priority = [category for category in ("etf", "fund", "stock", "index", "commodity", "fx", "crypto", "other") if category in order]
    for category in order:
        if category not in priority:
            priority.append(category)
    weighted_cycle: list[str] = []
    for category in priority:
        weighted_cycle.extend([category] * max(1, int(DISCOVERY_CATEGORY_WEIGHTS.get(category, 1))))
    while len(selected) < maximum:
        added = False
        for category in weighted_cycle:
            rows = buckets.get(category) or []
            index = next_index.get(category, 0)
            if index >= len(rows):
                continue
            selected.append(rows[index])
            next_index[category] = index + 1
            added = True
            if len(selected) >= maximum:
                break
        if not added:
            break
    return selected


def attach_relative_strength(results: list[dict[str, Any]]) -> None:
    if len(results) < 3:
        for item in results:
            item["relative_strength"] = None
        return
    ordered = sorted(enumerate(results), key=lambda pair: (float(pair[1].get("score") or 0.0), pair[0]))
    n = len(ordered)
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


def _category_from_item(item: dict[str, Any]) -> str:
    return str(item.get("category") or "other")


def _item_suitability_eligible(item: dict[str, Any]) -> bool:
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    value = metrics.get("suitability_eligible")
    return True if value is None else bool(value)


def _category_caps_for_risk(risk_tolerance: str) -> dict[str, float]:
    profile = CATEGORY_MAX_FRACTION_BY_RISK.get(risk_tolerance, CATEGORY_MAX_FRACTION_BY_RISK["medium"])
    return {key: _clip(float(value), 0.0, 1.0) for key, value in profile.items()}


def _capped_weight_allocations(total: float, weights: list[float], cap_amount: float, *, results: list[dict[str, Any]] | None = None, category_cap_fractions: dict[str, float] | None = None, budget: float | None = None) -> list[float]:
    n = len(weights)
    allocations = [0.0] * n
    active = {i for i, weight in enumerate(weights) if weight > 0}
    remaining = max(0.0, float(total))
    candidate_cap = max(0.0, float(cap_amount))
    category_totals: dict[str, float] = defaultdict(float)
    reference_budget = max(0.0, float(budget if budget is not None else total))

    def category_limit(i: int) -> float:
        if results is None or category_cap_fractions is None:
            return math.inf
        category = _category_from_item(results[i])
        fraction = category_cap_fractions.get(category, category_cap_fractions.get("other", 1.0))
        return max(0.0, reference_budget * _clip(float(fraction), 0.0, 1.0))

    while active and remaining > 1e-9:
        total_weight = sum(weights[i] for i in active)
        if total_weight <= 0:
            break
        proposed = {i: remaining * weights[i] / total_weight for i in active}
        scale = 1.0
        for i, share in proposed.items():
            if share > 0:
                room = max(0.0, candidate_cap - allocations[i])
                scale = min(scale, room / share)
        if results is not None and category_cap_fractions is not None:
            category_proposed: dict[str, float] = defaultdict(float)
            for i, share in proposed.items():
                category_proposed[_category_from_item(results[i])] += share
            for category, share in category_proposed.items():
                if share <= 0:
                    continue
                fraction = category_cap_fractions.get(category, category_cap_fractions.get("other", 1.0))
                limit = reference_budget * _clip(float(fraction), 0.0, 1.0)
                room = max(0.0, limit - category_totals[category])
                scale = min(scale, room / share)
        scale = _clip(scale, 0.0, 1.0)
        if scale <= 1e-12:
            removed = False
            for i in list(active):
                if allocations[i] >= candidate_cap - 1e-9:
                    active.remove(i)
                    removed = True
                    continue
                if results is not None and category_cap_fractions is not None:
                    category = _category_from_item(results[i])
                    if category_totals[category] >= category_limit(i) - 1e-9:
                        active.remove(i)
                        removed = True
            if not removed:
                break
            continue
        allocated_step = 0.0
        for i, share in proposed.items():
            increment = share * scale
            allocations[i] += increment
            allocated_step += increment
            if results is not None and category_cap_fractions is not None:
                category_totals[_category_from_item(results[i])] += increment
        remaining = max(0.0, remaining - allocated_step)
        if scale >= 1.0 - 1e-12:
            break
        for i in list(active):
            if allocations[i] >= candidate_cap - 1e-9:
                active.remove(i)
                continue
            if results is not None and category_cap_fractions is not None:
                category = _category_from_item(results[i])
                if category_totals[category] >= category_limit(i) - 1e-9:
                    active.remove(i)
    return allocations


def _whole_unit_allocations(results: list[dict[str, Any]], desired: list[float], weights: list[float], *, budget: float, target_deployable: float, cap_fraction: float, reserve_floor: float, cap_is_hard: bool, risk_tolerance: str, category_cap_fractions: dict[str, float] | None = None) -> tuple[list[float], dict[str, Any]]:
    n = len(results)
    allocations = [0.0] * n
    units = [0] * n
    max_total = max(0.0, budget * (1.0 - reserve_floor))
    target = min(max_total, max(0.0, float(target_deployable)))
    integer_ceiling = target
    automatic_candidate_cap = max(0.0, budget * cap_fraction)
    hard_candidate_limit = automatic_candidate_cap if cap_is_hard else math.inf
    category_caps = category_cap_fractions or _category_caps_for_risk(risk_tolerance)
    category_totals: dict[str, float] = defaultdict(float)

    prices: list[float] = []
    desired_amounts: list[float] = []
    for index, item in enumerate(results):
        try:
            price = float(item.get("portfolio_price") or item.get("price") or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        if not math.isfinite(price) or price <= 0:
            price = 0.0
        prices.append(price)
        try:
            wanted = float(desired[index]) if index < len(desired) else 0.0
        except (TypeError, ValueError):
            wanted = 0.0
        desired_amounts.append(max(0.0, wanted) if math.isfinite(wanted) else 0.0)

    for i in range(n):
        if i >= len(weights) or weights[i] <= 0:
            continue
        price = prices[i]
        wanted = desired_amounts[i]
        if price <= 0 or wanted <= 0:
            continue
        quantity = int(math.floor(wanted / price + 1e-12))
        if quantity <= 0:
            continue
        if math.isfinite(hard_candidate_limit):
            quantity = min(quantity, int(math.floor(hard_candidate_limit / price + 1e-12)))
        category = _category_from_item(results[i])
        category_fraction = category_caps.get(category, category_caps.get("other", 1.0))
        category_limit = budget * _clip(float(category_fraction), 0.0, 1.0)
        quantity = min(quantity, int(math.floor(max(0.0, category_limit - category_totals[category]) / price + 1e-12)))
        current_total = sum(allocations)
        quantity = min(quantity, int(math.floor(max(0.0, integer_ceiling - current_total) / price + 1e-12)))
        if quantity <= 0:
            continue
        units[i] = quantity
        allocations[i] = round(quantity * price, 2)
        category_totals[category] = round(category_totals[category] + allocations[i], 2)

    for _ in range(10000):
        current = round(sum(allocations), 2)
        best_index = None
        best_key = None
        for i in range(n):
            if i >= len(weights) or weights[i] <= 0:
                continue
            price = prices[i]
            if price <= 0 or current + price > integer_ceiling + 0.005:
                continue
            if math.isfinite(hard_candidate_limit) and allocations[i] + price > hard_candidate_limit + 0.005:
                continue
            category = _category_from_item(results[i])
            category_fraction = category_caps.get(category, category_caps.get("other", 1.0))
            category_limit = budget * _clip(float(category_fraction), 0.0, 1.0)
            if category_totals[category] + price > category_limit + 0.005:
                continue
            before = abs(desired_amounts[i] - allocations[i])
            after = abs(desired_amounts[i] - (allocations[i] + price))
            improvement = before - after
            if improvement <= 1e-9:
                continue
            key = (
                improvement,
                1 if units[i] == 0 else 0,
                float(weights[i]),
                float(results[i].get("score") or 0.0),
                float(results[i].get("confidence") or 0.0),
                -i,
            )
            if best_key is None or key > best_key:
                best_key = key
                best_index = i
        if best_index is None:
            break
        price = prices[best_index]
        category = _category_from_item(results[best_index])
        units[best_index] += 1
        allocations[best_index] = round(units[best_index] * price, 2)
        category_totals[category] = round(category_totals[category] + price, 2)

    deployed = round(sum(allocations), 2)
    return allocations, {
        "whole_unit_target": round(target, 2),
        "whole_unit_integer_ceiling": round(integer_ceiling, 2),
        "whole_unit_soft_candidate_cap": round(automatic_candidate_cap, 2),
        "whole_unit_cap_is_hard": bool(cap_is_hard),
        "whole_unit_auto_lot_override_candidates": 0,
        "whole_unit_auto_lot_override_used": 0,
        "whole_unit_adjustment": round(deployed - target, 2),
        "whole_unit_residual": round(max(0.0, target - deployed), 2),
        "whole_unit_positive_candidates": sum(1 for value in allocations if value > 0),
    }


def allocate_budget(results: list[dict[str, Any]], amount: float | None, *, risk_tolerance: str = "medium", diversification: str = "medium", min_confidence: float = 0.45, max_candidate_fraction: float | None = None, minimum_cash_reserve_fraction: float = 0.0, whole_units_only: bool = False) -> dict[str, Any]:
    if risk_tolerance not in RISK_PROFILES:
        risk_tolerance = "medium"
    if diversification not in DIVERSIFICATION_DEFAULT_MAX_FRACTION:
        diversification = "medium"
    profile = RISK_PROFILES[risk_tolerance]
    min_confidence = _clip(float(min_confidence), 0.0, 1.0)
    reserve_floor = _clip(float(minimum_cash_reserve_fraction), 0.0, 1.0)
    cap_is_hard = max_candidate_fraction is not None
    cap_fraction = DIVERSIFICATION_DEFAULT_MAX_FRACTION[diversification] if max_candidate_fraction is None else _clip(float(max_candidate_fraction), 0.01, 1.0)
    category_caps = _category_caps_for_risk(risk_tolerance)
    if amount is None:
        for item in results:
            item["suggested_amount"] = None
            item["suggested_units"] = None
        return {"budget": None, "deployed": None, "cash_reserve": None, "deployment_fraction": None, "max_candidate_fraction": cap_fraction, "minimum_cash_reserve_fraction": reserve_floor, "category_max_fractions": category_caps}
    budget = float(amount)
    if not math.isfinite(budget) or budget < 0:
        raise ValueError("Investment amount must be zero or greater")
    if budget == 0:
        for item in results:
            item["suggested_amount"] = 0.0
            item["suggested_units"] = 0.0
        return {"budget": 0.0, "deployed": 0.0, "cash_reserve": 0.0, "deployment_fraction": 0.0, "max_candidate_fraction": cap_fraction, "minimum_cash_reserve_fraction": reserve_floor, "category_max_fractions": category_caps}

    required_confidence = min_confidence
    full_confidence_target = max(min_confidence, float(profile["min_confidence"]))
    staged_score_floor = max(52.0, float(profile["min_score"]) - 8.0)
    allow_staged = risk_tolerance != "very_low"
    weights: list[float] = []
    qualities: list[float] = []
    for item in results:
        score = float(item.get("score") or 0.0)
        confidence = float(item.get("confidence") or 0.0)
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        regime = float(metrics.get("regime_score") or 0.0)
        suitability_eligible = _item_suitability_eligible(item)
        vol_ratio_raw = metrics.get("volatility_ratio")
        try:
            vol_ratio = float(vol_ratio_raw) if vol_ratio_raw is not None else 1.0
        except (TypeError, ValueError):
            vol_ratio = 1.0
        if not math.isfinite(vol_ratio) or vol_ratio <= 0:
            vol_ratio = 1.0
        risk_scale = _clip(1.0 / (max(0.60, vol_ratio) ** float(profile["allocation_vol_power"])), 0.25 if risk_tolerance in {"very_low", "low"} else 0.40, 1.25)
        relative = item.get("relative_strength")
        relative_scale = 1.0
        if relative is not None:
            try:
                relative_scale = 0.85 + 0.30 * _clip(float(relative) / 100.0, 0.0, 1.0)
            except (TypeError, ValueError):
                relative_scale = 1.0
        regime_ok = regime > float(profile["regime_floor"])
        full_eligible = suitability_eligible and score >= float(profile["min_score"]) and confidence >= full_confidence_target and regime_ok
        staged_eligible = allow_staged and suitability_eligible and not full_eligible and score >= staged_score_floor and confidence >= required_confidence and regime_ok
        if full_eligible:
            allocation_tier = "full"
            quality = _clip((score - float(profile["min_score"]) + 4.0) / 25.0, 0.0, 1.0) * _clip(confidence, 0.0, 1.0)
            weight = max(0.0, score - (float(profile["min_score"]) - 3.0)) * max(0.0, confidence) * risk_scale * relative_scale
        elif staged_eligible:
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
        item["suitability_eligible"] = suitability_eligible
        item["category_max_fraction"] = category_caps.get(_category_from_item(item), category_caps.get("other", 1.0))
        weights.append(weight)
        qualities.append(quality)

    top_quality = max(qualities, default=0.0)
    strongest_index = max(range(len(qualities)), key=lambda i: (qualities[i], -i), default=None)
    strongest = results[strongest_index] if strongest_index is not None and top_quality > 0 else None
    deployment_fraction = _clip(top_quality * float(profile["deployment_mult"]), 0.0, 1.0 - reserve_floor)
    deployable = round(budget * deployment_fraction, 2)
    cap_amount = round(budget * cap_fraction, 2)
    raw_allocations = _capped_weight_allocations(deployable, weights, cap_amount, results=results, category_cap_fractions=category_caps, budget=budget)
    rounded = [round(value, 2) for value in raw_allocations]
    excess = round(sum(rounded) - deployable, 2)
    if excess > 0 and rounded:
        idx = max(range(len(rounded)), key=lambda i: (rounded[i], -i))
        rounded[idx] = round(max(0.0, rounded[idx] - excess), 2)
    pre_whole_deployed = round(sum(rounded), 2)
    cap_limited = pre_whole_deployed + 0.005 < deployable
    whole_meta: dict[str, Any] = {}
    if whole_units_only:
        rounded, whole_meta = _whole_unit_allocations(results, rounded, weights, budget=budget, target_deployable=deployable, cap_fraction=cap_fraction, reserve_floor=reserve_floor, cap_is_hard=cap_is_hard, risk_tolerance=risk_tolerance, category_cap_fractions=category_caps)
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
    return {"budget": round(budget, 2), "deployed": deployed, "cash_reserve": reserve, "deployment_fraction": round(deployed / budget, 4) if budget > 0 else 0.0, "target_deployed": deployable, "target_deployment_fraction": round(deployment_fraction, 4), "pre_whole_deployed": pre_whole_deployed, "cap_limited": bool(cap_limited), "max_candidate_fraction": cap_fraction, "candidate_cap_is_hard": bool(cap_is_hard), "minimum_cash_reserve_fraction": reserve_floor, "risk_tolerance": risk_tolerance, "risk_deployment_multiplier": float(profile["deployment_mult"]), "diversification": diversification, "required_confidence": round(required_confidence, 4), "full_confidence_target": round(full_confidence_target, 4), "staged_score_floor": round(staged_score_floor, 2), "staged_allocations_allowed": bool(allow_staged), "strongest_quality": round(top_quality, 4), "strongest_score": None if strongest is None else round(float(strongest.get("score") or 0.0), 2), "strongest_confidence": None if strongest is None else round(float(strongest.get("confidence") or 0.0), 4), "strongest_tier": None if strongest is None else strongest.get("allocation_tier"), "whole_units_only": bool(whole_units_only), "category_max_fractions": category_caps, **whole_meta}


def sanitize_ai_ranking(results: list[dict[str, Any]], ranking: Any, amount: float | None, *, max_candidate_fraction: float = 1.0, minimum_cash_reserve_fraction: float = 0.0, whole_units_only: bool = False, max_candidate_fraction_is_hard: bool = True, risk_tolerance: str = "medium") -> dict[str, Any]:
    if risk_tolerance not in RISK_PROFILES:
        risk_tolerance = "medium"
    valid_actions = {"consider", "watch", "avoid"}
    known = {(str(item.get("provider") or ""), str(item.get("provider_id") or "")): item for item in results}
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
            if action == "buy":
                action = "consider"
            item["ai_action"] = action if action in valid_actions else "watch"
            item["ai_reason"] = str(raw.get("reason") or "").strip()[:500]
            deterministic_eligible = bool(item.get("allocation_eligible")) and _item_suitability_eligible(item)
            proposed = 0.0
            if amount is not None and deterministic_eligible and item["ai_action"] == "consider":
                try:
                    parsed = float(raw.get("suggested_amount"))
                    if math.isfinite(parsed) and parsed > 0:
                        proposed = parsed
                except (TypeError, ValueError):
                    pass
            if not deterministic_eligible and item["ai_action"] == "consider":
                item["ai_action"] = "watch"
            proposals.append((item, proposed))
    if amount is None:
        return {"budget": None, "deployed": None, "cash_reserve": None}
    budget = max(0.0, float(amount))
    reserve_floor = _clip(float(minimum_cash_reserve_fraction), 0.0, 1.0)
    max_deployable = budget * (1.0 - reserve_floor)
    cap_fraction = _clip(float(max_candidate_fraction), 0.01, 1.0)
    cap_amount = budget * cap_fraction
    category_caps = _category_caps_for_risk(risk_tolerance)
    proposal_items = [item for item, _ in proposals]
    proposal_weights = [max(0.0, value) for _, value in proposals]
    requested_total = min(max_deployable, sum(proposal_weights))
    allocations = _capped_weight_allocations(requested_total, proposal_weights, cap_amount, results=proposal_items, category_cap_fractions=category_caps, budget=budget)
    allocations = [round(value, 2) for value in allocations]
    target_deployed = round(sum(allocations), 2)
    whole_meta: dict[str, Any] = {}
    if whole_units_only:
        ai_weights = [(max(0.0, float(item.get("ai_score") or 0.0)) + max(0.0, proposed) / max(1.0, budget)) if proposed > 0 else 0.0 for item, proposed in proposals]
        allocations, whole_meta = _whole_unit_allocations(proposal_items, allocations, ai_weights, budget=budget, target_deployable=target_deployed, cap_fraction=cap_fraction, reserve_floor=reserve_floor, cap_is_hard=max_candidate_fraction_is_hard, risk_tolerance=risk_tolerance, category_cap_fractions=category_caps)
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
    return {"budget": round(budget, 2), "deployed": deployed, "cash_reserve": round(max(0.0, budget - deployed), 2), "target_deployed": target_deployed, "target_deployment_fraction": round(target_deployed / budget, 4) if budget > 0 else 0.0, "max_candidate_fraction": cap_fraction, "candidate_cap_is_hard": bool(max_candidate_fraction_is_hard), "minimum_cash_reserve_fraction": reserve_floor, "whole_units_only": bool(whole_units_only), "category_max_fractions": category_caps, **whole_meta}
