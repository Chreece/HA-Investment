"""Validated deterministic portfolio-construction contract.

This module is the production-transfer target for the V10 -> V12c -> V13
research line.  It is deliberately pure: no Home Assistant state and no network
calls live here.

The validated contract separates:
* horizon-specific market signal from user risk tolerance;
* economic exposure sleeves from product wrappers;
* market activation from portfolio risk controls;
* exact model weights from monetary/lot projection.

V13 validated the activation-proportional within-sleeve blend only for the
frozen adaptive signal scaffold.  Runtime wiring is intentionally a separate
step so these helpers can be parity-tested before recommendation behaviour is
changed.
"""
from __future__ import annotations

from collections import defaultdict
import datetime as dt
import math
import re
import statistics
from typing import Any, Iterable

RISK_ORDER = ("very_low", "low", "medium", "high", "very_high")
SLEEVE_ORDER = (
    "cash_like",
    "government_bond",
    "aggregate_bond",
    "broad_equity",
    "sector_equity",
    "single_equity",
    "commodity",
    "crypto",
)

ECONOMIC_SLEEVE_CAPS: dict[str, dict[str, float]] = {
    "very_low": {
        "cash_like": 0.55,
        "government_bond": 0.35,
        "aggregate_bond": 0.30,
        "broad_equity": 0.20,
        "sector_equity": 0.05,
        "single_equity": 0.05,
        "commodity": 0.05,
        "crypto": 0.00,
    },
    "low": {
        "cash_like": 0.50,
        "government_bond": 0.45,
        "aggregate_bond": 0.40,
        "broad_equity": 0.35,
        "sector_equity": 0.10,
        "single_equity": 0.15,
        "commodity": 0.10,
        "crypto": 0.03,
    },
    "medium": {
        "cash_like": 0.45,
        "government_bond": 0.45,
        "aggregate_bond": 0.45,
        "broad_equity": 0.55,
        "sector_equity": 0.25,
        "single_equity": 0.30,
        "commodity": 0.15,
        "crypto": 0.10,
    },
    "high": {
        "cash_like": 0.40,
        "government_bond": 0.40,
        "aggregate_bond": 0.45,
        "broad_equity": 0.70,
        "sector_equity": 0.45,
        "single_equity": 0.50,
        "commodity": 0.25,
        "crypto": 0.25,
    },
    "very_high": {
        "cash_like": 0.35,
        "government_bond": 0.35,
        "aggregate_bond": 0.40,
        "broad_equity": 0.85,
        "sector_equity": 0.70,
        "single_equity": 0.75,
        "commodity": 0.40,
        "crypto": 0.40,
    },
}

PORTFOLIO_VOL_TARGET = {
    "very_low": 0.05,
    "low": 0.10,
    "medium": 0.16,
    "high": 0.25,
    "very_high": 0.40,
}
NORMAL_ES95_MULT = 2.0627128075074257
PORTFOLIO_WEEKLY_ES95_TARGET = {
    risk: NORMAL_ES95_MULT * vol / math.sqrt(52.0)
    for risk, vol in PORTFOLIO_VOL_TARGET.items()
}

ACTIVATION_FLOOR = 48.0
ACTIVATION_FULL = 75.0
MIN_RISK_HISTORY_WEEKS = 52
RISK_TOLERANCE = 1.01
EPS = 1e-9
SIGNAL_SCAFFOLD_RISK = "very_high"
VALIDATED_SIGNAL_STRATEGY = "adaptive"

_WORD_RE = re.compile(r"[^A-Z0-9]+")


def _sf(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _symbol(item: dict[str, Any]) -> str:
    return str(item.get("symbol") or item.get("provider_id") or "").strip().upper()


def _identity(item: dict[str, Any]) -> tuple[str, str, str]:
    """Stable production identity without collapsing cross-provider duplicates."""
    provider = str(item.get("provider") or "").strip().lower()
    provider_id = str(item.get("provider_id") or "").strip()
    if provider or provider_id:
        return (provider, provider_id, "")
    return ("", "", _symbol(item))


def _words(value: Any) -> str:
    return _WORD_RE.sub(" ", str(value or "").upper()).strip()


def classify_economic_exposure(asset: dict[str, Any]) -> dict[str, Any]:
    """Return a conservative economic sleeve independent from wrapper category.

    Unknown ETF/fund exposures are deliberately not allocatable by the validated
    core.  A wrong sleeve can defeat the risk envelope; leaving cash cannot.
    """
    category = str(asset.get("category") or "other").strip().lower()
    symbol = _symbol(asset)
    name = _words(asset.get("name"))

    if category == "index" or symbol.startswith("^"):
        return {"economic_subtype": "benchmark", "economic_sleeve": "nontradable", "allocatable": False}
    if symbol.endswith("=F"):
        return {"economic_subtype": "commodity_future", "economic_sleeve": "nontradable", "allocatable": False}
    if category == "fx" or symbol.endswith("=X"):
        return {"economic_subtype": "currency", "economic_sleeve": "nontradable", "allocatable": False}
    if category == "crypto":
        return {"economic_subtype": "crypto", "economic_sleeve": "crypto", "allocatable": True}
    if category == "stock":
        return {"economic_subtype": "single_equity", "economic_sleeve": "single_equity", "allocatable": True}
    if category == "commodity":
        return {"economic_subtype": "broad_commodity", "economic_sleeve": "commodity", "allocatable": True}

    if category not in {"etf", "fund"}:
        return {"economic_subtype": "unknown", "economic_sleeve": "unknown", "allocatable": False}

    # Canonical no-key discovery fallbacks whose display names do not carry
    # enough taxonomy words to classify safely by name alone.  These are
    # wrapper-identity facts, not fitted score/signal parameters.
    if symbol == "QQQ":
        return {"economic_subtype": "equity_satellite", "economic_sleeve": "sector_equity", "allocatable": True}
    if symbol in {"VFIAX", "FXAIX"}:
        return {"economic_subtype": "broad_equity", "economic_sleeve": "broad_equity", "allocatable": True}

    cash_terms = (
        "OVERNIGHT",
        "MONEY MARKET",
        "ULTRASHORT",
        "ULTRA SHORT",
        "ENHANCED SHORT MATURITY",
        "0 1 YEAR",
        "0 1YR",
        "1 3 MONTH T BILL",
        "TREASURY BILL",
        "T BILL",
        "ESTR",
        "STR DAILY",
    )
    if any(term in name for term in cash_terms):
        return {"economic_subtype": "cash_like", "economic_sleeve": "cash_like", "allocatable": True}

    government_terms = (
        "TREASURY",
        "GOVT BOND",
        "GOVERNMENT BOND",
        "GOV BOND",
        "SOVEREIGN",
    )
    if any(term in name for term in government_terms):
        return {"economic_subtype": "government_bond", "economic_sleeve": "government_bond", "allocatable": True}

    bond_terms = (
        "BOND",
        "CORPORATE",
        "HIGH YIELD",
        "AGGREGATE",
        "CREDIT",
    )
    if any(term in name for term in bond_terms):
        return {"economic_subtype": "aggregate_bond", "economic_sleeve": "aggregate_bond", "allocatable": True}

    satellite_terms = (
        "NASDAQ 100",
        "GROWTH",
        "SMALL CAP",
        "RUSSELL 2000",
        "TECHNOLOGY",
        "SEMICONDUCTOR",
        "SECTOR",
        "REAL ESTATE",
        "REIT",
    )
    if any(term in name for term in satellite_terms):
        return {"economic_subtype": "equity_satellite", "economic_sleeve": "sector_equity", "allocatable": True}

    broad_terms = (
        "S P 500",
        "S P 1500",
        "TOTAL STOCK MARKET",
        "TOTAL MARKET",
        "US EQUITY",
        "USA",
        "ALL WORLD",
        "ACWI",
        "GLOBAL EQUITY",
        "MSCI WORLD",
        "DEVELOPED",
        "EMERGING",
        "EAFE",
        "WORLD",
    )
    if any(term in name for term in broad_terms):
        return {"economic_subtype": "broad_equity", "economic_sleeve": "broad_equity", "allocatable": True}

    if any(term in name for term in ("GOLD", "SILVER", "COMMODITY", "GSCI")):
        return {"economic_subtype": "commodity", "economic_sleeve": "commodity", "allocatable": True}

    return {"economic_subtype": "unknown", "economic_sleeve": "unknown", "allocatable": False}


def activation(score: float, reliability: float) -> float:
    """Frozen V10 activation mapping; reliability may only scale down."""
    score = _sf(score)
    if score <= ACTIVATION_FLOOR:
        return 0.0
    strength = _clip((score - ACTIVATION_FLOOR) / (ACTIVATION_FULL - ACTIVATION_FLOOR))
    return strength * _clip(_sf(reliability))


def _annualize_half_year(value: Any) -> float | None:
    try:
        ret = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ret) or ret <= -1.0:
        return None
    return (1.0 + ret) ** 2 - 1.0


def restore_scaffold_market_score(item: dict[str, Any]) -> float:
    """Undo portfolio-context penalties from the fixed signal scaffold.

    This is the V1/V10 ``restore_market_score`` transfer.  Concentration and
    overlap are useful user constraints, but they are not market signal and
    therefore cannot make the market score depend on the user's portfolio.
    """
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    final = _sf(item.get("score"))
    confidence = _clip(_sf(item.get("confidence")))
    signal = _sf(metrics.get("signal_score_before_suitability"), final)
    concentration = max(0.0, _sf(metrics.get("concentration_penalty")))
    overlap = max(0.0, _sf(metrics.get("overlap_score_penalty")))
    score = _clip(
        signal + confidence * (12.0 * concentration + overlap),
        0.0,
        100.0,
    )
    return min(score, 47.0) if metrics.get("suitability_eligible") is False else score


def validated_market_score(
    scaffold_result: dict[str, Any],
    economic_sleeve: str,
) -> float:
    """Frozen V10 risk-invariant market score from one fixed-risk scaffold.

    The Home Assistant scorer is still used to derive the horizon signal and
    reliability fields, but it must be invoked with ``SIGNAL_SCAFFOLD_RISK``.
    User risk never enters this function.
    """
    metrics = (
        scaffold_result.get("metrics")
        if isinstance(scaffold_result.get("metrics"), dict)
        else {}
    )
    generic = restore_scaffold_market_score(scaffold_result)
    if economic_sleeve not in {
        "cash_like",
        "government_bond",
        "aggregate_bond",
    }:
        return round(_clip(generic, 0.0, 100.0), 2)

    # Exact V1 defensive-exposure repair used by V10/V11/V12c/V13.
    if metrics.get("suitability_eligible") is False:
        return round(min(generic, 47.0), 2)
    confidence = _clip(_sf(scaffold_result.get("confidence")))
    vol = max(0.0, _sf(metrics.get("annualized_volatility")))
    drawdown = abs(min(0.0, _sf(metrics.get("max_drawdown"))))
    r126 = metrics.get("return_126d")
    carry = _annualize_half_year(r126)

    if economic_sleeve == "cash_like":
        score = (
            52.0
            + 14.0 * _clip((carry or 0.0) / 0.04)
            + 10.0 * _clip(1.0 - vol / 0.02)
            + 8.0 * _clip(1.0 - drawdown / 0.03)
            + 6.0 * confidence
        )
        score = min(score, 86.0)
    elif economic_sleeve == "government_bond":
        score = (
            48.0
            + 14.0 * _clip((carry or 0.0) / 0.05)
            + 10.0 * _clip(1.0 - vol / 0.08)
            + 8.0 * _clip(1.0 - drawdown / 0.15)
            + 8.0 * _clip(_sf(r126) / 0.08, -1.0, 1.0)
            + 6.0 * confidence
        )
    else:
        score = (
            46.0
            + 12.0 * _clip((carry or 0.0) / 0.06)
            + 9.0 * _clip(1.0 - vol / 0.10)
            + 8.0 * _clip(1.0 - drawdown / 0.22)
            + 10.0 * _clip(_sf(r126) / 0.10, -1.0, 1.0)
            + 6.0 * confidence
        )
    return round(_clip(score, 0.0, 100.0), 2)


def weekly_return_map_from_points(
    points: Iterable[Any],
    *,
    lookback_days: int = 3 * 370,
) -> dict[str, float]:
    """Convert timestamp/value points to the V10 rolling weekly risk map.

    ``points`` may be ``HistoryPoint`` objects, ``(ts, value)`` tuples, or
    dictionaries with ``ts``/``value``.  The last observation of each ISO week
    is used and only the trailing V10 three-year risk window is retained.
    """
    normalized: list[tuple[int, float]] = []
    for raw in points:
        if isinstance(raw, dict):
            ts, value = raw.get("ts"), raw.get("value")
        elif isinstance(raw, (tuple, list)) and len(raw) >= 2:
            ts, value = raw[0], raw[1]
        else:
            ts, value = getattr(raw, "ts", None), getattr(raw, "value", None)
        try:
            stamp = int(ts)
            price = float(value)
        except (TypeError, ValueError):
            continue
        if stamp <= 0 or not math.isfinite(price) or price <= 0:
            continue
        normalized.append((stamp, price))
    if len(normalized) < 2:
        return {}
    normalized.sort(key=lambda row: row[0])
    latest = normalized[-1][0]
    cutoff = latest - max(1, int(lookback_days)) * 86400
    weekly_last: dict[tuple[int, int], tuple[int, float]] = {}
    for stamp, price in normalized:
        if stamp < cutoff:
            continue
        date = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).date()
        iso = date.isocalendar()
        weekly_last[(iso.year, iso.week)] = (stamp, price)
    ordered = sorted(weekly_last.items(), key=lambda row: row[1][0])
    out: dict[str, float] = {}
    for index in range(1, len(ordered)):
        previous = ordered[index - 1][1][1]
        if previous <= 0:
            continue
        (year, week), (_, value) = ordered[index]
        out[f"{year:04d}-W{week:02d}"] = value / previous - 1.0
    return out


def context_allocation_scale(scaffold_result: dict[str, Any], market_score: float) -> float:
    """Translate existing portfolio penalties into a downward-only constraint.

    V10 restores these penalties out of market score.  Production still needs
    the user's concentration/overlap preferences, so their previous score
    effect is retained only as a post-model scale that can never increase a
    validated weight.
    """
    metrics = (
        scaffold_result.get("metrics")
        if isinstance(scaffold_result.get("metrics"), dict)
        else {}
    )
    confidence = _clip(_sf(scaffold_result.get("confidence")))
    concentration = max(0.0, _sf(metrics.get("concentration_penalty")))
    overlap = max(0.0, _sf(metrics.get("overlap_score_penalty")))
    penalty = confidence * (12.0 * concentration + overlap)
    base = activation(market_score, confidence)
    if base <= EPS:
        return 0.0
    constrained = activation(max(0.0, market_score - penalty), confidence)
    return _clip(constrained / base)


def prepare_scored_candidate(
    scaffold_result: dict[str, Any],
    asset: dict[str, Any],
    risk_weekly_returns: dict[str, float],
) -> dict[str, Any]:
    """Attach the frozen market score and production risk inputs to a result."""
    item = dict(scaffold_result)
    classification = classify_economic_exposure({**asset, **item})
    item["economic_subtype"] = classification["economic_subtype"]
    item["economic_sleeve"] = classification["economic_sleeve"]
    item["allocatable_economic_exposure"] = bool(classification["allocatable"])
    market_score = validated_market_score(item, item["economic_sleeve"])
    confidence = _clip(_sf(item.get("confidence")))
    act = activation(market_score, confidence)
    risk_map = dict(risk_weekly_returns or {})
    risk_eligible = len(risk_map) >= MIN_RISK_HISTORY_WEEKS
    if not classification["allocatable"] or not risk_eligible:
        act = 0.0

    scaffold_score = _sf(item.get("score"))
    item["scaffold_score"] = scaffold_score
    item["market_score"] = market_score
    item["score"] = market_score
    item["activation"] = act
    item["risk_weekly_returns"] = risk_map
    item["risk_history_weeks"] = len(risk_map)
    item["risk_history_eligible"] = risk_eligible
    item["allocation_context_scale"] = context_allocation_scale(item, market_score)
    if market_score >= 75.0:
        item["label"] = "strong_candidate"
    elif market_score >= 62.0:
        item["label"] = "candidate"
    elif market_score >= ACTIVATION_FLOOR:
        item["label"] = "watch"
    else:
        item["label"] = "caution"

    metrics = dict(item.get("metrics") or {})
    metrics.update(
        {
            "signal_scaffold_risk": SIGNAL_SCAFFOLD_RISK,
            "market_score_risk_profile_invariant": True,
            "economic_subtype": item["economic_subtype"],
            "economic_sleeve": item["economic_sleeve"],
            "risk_history_weeks": len(risk_map),
            "risk_history_eligible": risk_eligible,
            "validated_market_score": market_score,
            "validated_activation": act,
            "allocation_context_scale": item["allocation_context_scale"],
        }
    )
    item["metrics"] = metrics
    warnings = [
        str(value)
        for value in (item.get("warnings") or [])
        if str(value)
        not in {"volatility_high_for_selected_risk", "drawdown_high_for_selected_risk"}
    ]
    if not risk_eligible:
        warnings.append("validated_risk_history_insufficient")
    item["warnings"] = list(dict.fromkeys(warnings))
    return item


def _ordered_candidates(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        candidates,
        key=lambda item: (
            _sf(item.get("market_score")),
            _sf(item.get("confidence")),
            _symbol(item),
        ),
        reverse=True,
    )


def corrected_blend_base_weights(
    candidates: Iterable[dict[str, Any]],
    risk: str,
) -> list[tuple[dict[str, Any], float]]:
    """V12c-corrected V11 weights before portfolio-risk scaling."""
    if risk not in ECONOMIC_SLEEVE_CAPS:
        raise ValueError(f"Unsupported risk tolerance: {risk}")

    ordered = _ordered_candidates(candidates)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    winners: dict[str, dict[str, Any]] = {}
    for item in ordered:
        sleeve = str(item.get("economic_sleeve") or "unknown")
        act = max(0.0, _sf(item.get("activation")))
        if sleeve not in SLEEVE_ORDER or act <= EPS:
            continue
        grouped[sleeve].append(item)
        winners.setdefault(sleeve, item)

    weighted: list[tuple[dict[str, Any], float]] = []
    caps = ECONOMIC_SLEEVE_CAPS[risk]
    for sleeve in SLEEVE_ORDER:
        rows = grouped.get(sleeve) or []
        winner = winners.get(sleeve)
        if not rows or winner is None:
            continue
        sleeve_total = _clip(_sf(caps.get(sleeve))) * _clip(_sf(winner.get("activation")))
        if sleeve_total <= EPS:
            continue
        activation_total = sum(max(0.0, _sf(item.get("activation"))) for item in rows)
        if activation_total <= EPS:
            continue
        for item in rows:
            act = max(0.0, _sf(item.get("activation")))
            if act <= EPS:
                continue
            weight = sleeve_total * act / activation_total
            if weight > EPS:
                weighted.append((item, weight))

    total = sum(weight for _, weight in weighted)
    if total > 1.0 + 1e-12:
        weighted = [(item, weight / total) for item, weight in weighted]
    return weighted


def sleeve_totals(weighted: Iterable[tuple[dict[str, Any], float]]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for item, weight in weighted:
        totals[str(item.get("economic_sleeve") or "unknown")] += _sf(weight)
    return dict(totals)


def portfolio_weekly_returns(
    weighted: Iterable[tuple[dict[str, Any], float]],
) -> list[float]:
    """Build portfolio returns with missing observations treated as cash."""
    maps: list[tuple[float, dict[str, float]]] = []
    for item, weight in weighted:
        if weight <= EPS:
            continue
        raw = item.get("risk_weekly_returns")
        if not isinstance(raw, dict) or not raw:
            continue
        clean = {
            str(key): _sf(value)
            for key, value in raw.items()
            if math.isfinite(_sf(value, math.nan))
        }
        if clean:
            maps.append((float(weight), clean))
    if not maps:
        return []
    weeks = sorted(set().union(*(set(values) for _, values in maps)))
    return [sum(weight * values.get(week, 0.0) for weight, values in maps) for week in weeks]


def expected_shortfall_loss(returns: list[float]) -> float | None:
    if len(returns) < 30:
        return None
    ordered = sorted(returns)
    count = max(1, int(math.ceil(0.05 * len(ordered))))
    return max(0.0, -statistics.fmean(ordered[:count]))


def path_max_drawdown_loss(returns: list[float]) -> float | None:
    if not returns:
        return None
    value = 1.0
    peak = 1.0
    worst = 0.0
    for ret in returns:
        value *= 1.0 + ret
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return abs(worst)


def risk_signature(weighted: Iterable[tuple[dict[str, Any], float]]) -> dict[str, Any]:
    returns = portfolio_weekly_returns(weighted)
    if len(returns) < MIN_RISK_HISTORY_WEEKS:
        return {
            "annualized_volatility_3y": None,
            "expected_shortfall_95_weekly_3y": None,
            "max_drawdown_3y": None,
            "weekly_observations_3y": len(returns),
        }
    return {
        "annualized_volatility_3y": statistics.pstdev(returns) * math.sqrt(52.0),
        "expected_shortfall_95_weekly_3y": expected_shortfall_loss(returns),
        "max_drawdown_3y": path_max_drawdown_loss(returns),
        "weekly_observations_3y": len(returns),
    }


def within_risk_target(signature: dict[str, Any], risk: str) -> bool:
    vol = signature.get("annualized_volatility_3y")
    es = signature.get("expected_shortfall_95_weekly_3y")
    if vol is None or es is None:
        return False
    return (
        _sf(vol) <= PORTFOLIO_VOL_TARGET[risk] * RISK_TOLERANCE
        and _sf(es) <= PORTFOLIO_WEEKLY_ES95_TARGET[risk] * RISK_TOLERANCE
    )


def scale_down_to_risk_contract(
    weighted: list[tuple[dict[str, Any], float]],
    risk: str,
) -> tuple[list[tuple[dict[str, Any], float]], dict[str, Any]]:
    """Frozen V10 portfolio vol/ES envelope.  Never increases any risk weight."""
    if risk not in PORTFOLIO_VOL_TARGET:
        raise ValueError(f"Unsupported risk tolerance: {risk}")
    if not weighted:
        empty = risk_signature([])
        return [], {"risk_scale": 0.0, "pre": empty, "post": empty}

    pre = risk_signature(weighted)
    if within_risk_target(pre, risk):
        return weighted, {"risk_scale": 1.0, "pre": pre, "post": pre}

    cash = [(item, weight) for item, weight in weighted if item.get("economic_sleeve") == "cash_like"]
    risky = [(item, weight) for item, weight in weighted if item.get("economic_sleeve") != "cash_like"]

    def trial(factor: float, include_cash: bool = True) -> list[tuple[dict[str, Any], float]]:
        rows: list[tuple[dict[str, Any], float]] = []
        if include_cash:
            rows.extend(cash)
        rows.extend((item, weight * factor) for item, weight in risky)
        return rows

    lo, hi = 0.0, 1.0
    feasible: tuple[list[tuple[dict[str, Any], float]], dict[str, Any], float] | None = None
    for _ in range(60):
        mid = (lo + hi) / 2.0
        rows = trial(mid)
        signature = risk_signature(rows)
        if within_risk_target(signature, risk):
            feasible = (rows, signature, mid)
            lo = mid
        else:
            hi = mid
    if feasible is not None:
        rows, post, factor = feasible
        return rows, {"risk_scale": factor, "pre": pre, "post": post}

    lo, hi = 0.0, 1.0
    feasible = ([], risk_signature([]), 0.0)
    for _ in range(60):
        mid = (lo + hi) / 2.0
        rows = [(item, weight * mid) for item, weight in weighted]
        signature = risk_signature(rows)
        if within_risk_target(signature, risk):
            feasible = (rows, signature, mid)
            lo = mid
        else:
            hi = mid
    rows, post, factor = feasible
    return rows, {
        "risk_scale": factor,
        "pre": pre,
        "post": post,
        "scaled_cash_like_too": True,
    }


def constrained_cent_projection(
    candidates: Iterable[dict[str, Any]],
    weighted: Iterable[tuple[dict[str, Any], float]],
    risk: str,
    budget: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """V12b/V12c integer-cent projection, preserving every sleeve cap."""
    if budget <= 0:
        raise ValueError("Budget must be positive")
    budget_cents = int(round(budget * 100))
    caps = ECONOMIC_SLEEVE_CAPS[risk]

    groups: dict[str, list[tuple[dict[str, Any], float]]] = defaultdict(list)
    for item, weight in weighted:
        if weight > EPS:
            groups[str(item.get("economic_sleeve") or "unknown")].append((item, float(weight)))

    cents_by_identity: dict[tuple[str, str, str], int] = defaultdict(int)
    projection_rows: list[dict[str, Any]] = []
    total_projected = 0

    for sleeve, rows in sorted(groups.items()):
        exact_cents = [max(0.0, weight * budget * 100.0) for _, weight in rows]
        exact_total = sum(exact_cents)
        cap_fraction = max(0.0, _sf(caps.get(sleeve), 0.0))
        cap_cents = int(math.floor(cap_fraction * budget * 100.0 + 1e-9))
        exact_floor_cents = int(math.floor(exact_total + 1e-9))
        target_cents = min(exact_floor_cents, cap_cents)
        bases = [int(math.floor(value + 1e-12)) for value in exact_cents]

        if sum(bases) > target_cents:
            removal_order = sorted(
                range(len(rows)),
                key=lambda i: (exact_cents[i] - math.floor(exact_cents[i]), _symbol(rows[i][0])),
            )
            excess = sum(bases) - target_cents
            for i in removal_order:
                if excess <= 0:
                    break
                take = min(excess, bases[i])
                bases[i] -= take
                excess -= take

        remaining = target_cents - sum(bases)
        remainder_order = sorted(
            range(len(rows)),
            key=lambda i: (exact_cents[i] - math.floor(exact_cents[i]), _symbol(rows[i][0])),
            reverse=True,
        )
        for i in remainder_order:
            if remaining <= 0:
                break
            bases[i] += 1
            remaining -= 1

        projected_sleeve = sum(bases)
        if projected_sleeve > cap_cents or projected_sleeve > exact_floor_cents:
            raise AssertionError(f"Unsafe cent projection in sleeve {sleeve}")
        for (item, _), cents in zip(rows, bases, strict=True):
            cents_by_identity[_identity(item)] += cents
        total_projected += projected_sleeve
        projection_rows.append(
            {
                "sleeve": sleeve,
                "exact_weight": exact_total / (budget * 100.0),
                "exact_cents": exact_total,
                "exact_floor_cents": exact_floor_cents,
                "cap_fraction": cap_fraction,
                "cap_cents": cap_cents,
                "projected_cents": projected_sleeve,
            }
        )

    if total_projected > budget_cents:
        raise AssertionError("Projected total exceeds budget")

    out = [dict(item) for item in candidates]
    for item in out:
        cents = cents_by_identity.get(_identity(item), 0)
        amount = cents / 100.0
        item["suggested_amount"] = amount
        price = _sf(item.get("portfolio_price") or item.get("price"))
        item["suggested_units"] = amount / price if price > 0 else 0.0
        item["allocation_weight_validated"] = amount / budget
    return out, {
        "budget_cents": budget_cents,
        "projected_total_cents": total_projected,
        "sleeves": projection_rows,
    }


def validated_exact_weights(
    candidates: Iterable[dict[str, Any]],
    risk: str,
) -> tuple[list[tuple[dict[str, Any], float]], dict[str, Any]]:
    """Construct corrected V11 weights and apply the frozen V10 risk envelope."""
    prepared: list[dict[str, Any]] = []
    for raw in candidates:
        item = dict(raw)
        classification = classify_economic_exposure(item)
        # Production classification is authoritative.  A stale/malicious caller
        # cannot smuggle an ETF, reference series, or unknown wrapper into a
        # different sleeve by pre-populating these fields.
        item["economic_subtype"] = classification["economic_subtype"]
        item["economic_sleeve"] = classification["economic_sleeve"]
        if not classification["allocatable"] and item.get("economic_sleeve") not in SLEEVE_ORDER:
            item["activation"] = 0.0
        else:
            item["activation"] = activation(
                _sf(item.get("market_score")),
                _sf(item.get("confidence")),
            )
        risk_map = item.get("risk_weekly_returns")
        if not isinstance(risk_map, dict) or len(risk_map) < MIN_RISK_HISTORY_WEEKS:
            item["activation"] = 0.0
        prepared.append(item)

    pre = corrected_blend_base_weights(prepared, risk)
    post, risk_meta = scale_down_to_risk_contract(pre, risk)
    return post, {"candidates": prepared, **risk_meta}


def apply_downward_weight_constraints(
    weighted: Iterable[tuple[dict[str, Any], float]],
    *,
    risk: str | None = None,
    min_confidence: float = 0.0,
    max_candidate_fraction: float | None = None,
    minimum_cash_reserve_fraction: float = 0.0,
) -> tuple[list[tuple[dict[str, Any], float]], dict[str, Any]]:
    """Apply user/runtime constraints without ever increasing validated weights."""
    confidence_floor = _clip(_sf(min_confidence))
    candidate_cap = (
        None
        if max_candidate_fraction is None
        else _clip(_sf(max_candidate_fraction), 0.0, 1.0)
    )
    reserve_floor = _clip(_sf(minimum_cash_reserve_fraction))

    before = [(item, max(0.0, _sf(weight))) for item, weight in weighted if _sf(weight) > EPS]
    constrained: list[tuple[dict[str, Any], float]] = []
    removed_by_confidence: list[str] = []
    context_reduced: list[str] = []
    candidate_capped: list[str] = []

    for item, weight in before:
        if _clip(_sf(item.get("confidence"))) + 1e-15 < confidence_floor:
            removed_by_confidence.append(_symbol(item))
            continue
        scale = _clip(_sf(item.get("allocation_context_scale"), 1.0))
        adjusted = weight * scale
        if adjusted + 1e-15 < weight:
            context_reduced.append(_symbol(item))
        if candidate_cap is not None and adjusted > candidate_cap:
            adjusted = candidate_cap
            candidate_capped.append(_symbol(item))
        if adjusted > EPS:
            constrained.append((item, adjusted))

    pre_reserve_total = sum(weight for _, weight in constrained)
    max_deployed_fraction = max(0.0, 1.0 - reserve_floor)
    reserve_scale = 1.0
    if pre_reserve_total > max_deployed_fraction + 1e-15 and pre_reserve_total > EPS:
        reserve_scale = max_deployed_fraction / pre_reserve_total
        constrained = [(item, weight * reserve_scale) for item, weight in constrained]

    # Component-wise reductions can still remove a negatively correlated hedge.
    # Re-run the frozen portfolio envelope after every optional/user constraint;
    # this second pass may only scale down further.
    post_constraint_risk_meta: dict[str, Any] | None = None
    if risk is not None:
        if risk not in RISK_ORDER:
            raise ValueError(f"Unsupported risk tolerance: {risk}")
        constrained, post_constraint_risk_meta = scale_down_to_risk_contract(
            constrained, risk
        )

    # This is a safety contract, not just a diagnostic.
    before_map = {_identity(item): weight for item, weight in before}
    for item, weight in constrained:
        if weight > before_map.get(_identity(item), 0.0) + 1e-12:
            raise AssertionError("A production constraint increased a validated weight")
    if risk is not None and constrained:
        signature = risk_signature(constrained)
        if not within_risk_target(signature, risk):
            raise AssertionError("Post-constraint weights exceed the validated risk contract")

    return constrained, {
        "minimum_confidence": confidence_floor,
        "max_candidate_fraction": candidate_cap,
        "minimum_cash_reserve_fraction": reserve_floor,
        "removed_by_confidence": removed_by_confidence,
        "context_reduced": context_reduced,
        "candidate_capped": candidate_capped,
        "pre_constraint_weight": sum(weight for _, weight in before),
        "pre_reserve_weight": pre_reserve_total,
        "reserve_scale": reserve_scale,
        "post_constraint_weight": sum(weight for _, weight in constrained),
        "post_constraint_risk": post_constraint_risk_meta,
    }


def _whole_unit_category_set(values: Iterable[str] | None) -> set[str]:
    """Normalize the optional per-wrapper whole-unit selection."""
    return {str(value).strip().lower() for value in (values or []) if str(value).strip()}


def _whole_unit_required(
    item: dict[str, Any],
    whole_units_only: bool,
    whole_unit_categories: set[str],
) -> bool:
    """Return whether this literal candidate must be projected as whole units."""
    return bool(
        whole_units_only
        or item.get("whole_units_only")
        or str(item.get("category") or "other").strip().lower() in whole_unit_categories
    )


def enforce_discrete_risk_contract(
    results: list[dict[str, Any]],
    risk: str,
    budget: float,
    *,
    amount_key: str = "suggested_amount",
    units_key: str = "suggested_units",
    whole_units_only: bool = False,
    whole_unit_categories: Iterable[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Verify money/lot output and reduce it if discretisation breaks risk.

    Cent rounding, whole-unit flooring, or AI removal can selectively remove a
    hedge even though no individual allocation increased.  The continuous V10
    envelope is therefore checked again on the literal amounts returned to the
    user.  Any repair is a common downward ceiling from the already-approved
    output; every candidate is verified not to increase.
    """
    if risk not in RISK_ORDER:
        raise ValueError(f"Unsupported risk tolerance: {risk}")
    if budget <= 0:
        return results, {
            "risk_scale": 0.0,
            "pre": risk_signature([]),
            "post": risk_signature([]),
            "zero_deployment_safe": True,
            "adjusted": False,
        }

    whole_categories = _whole_unit_category_set(whole_unit_categories)
    original = [dict(item) for item in results]
    original_amounts = [max(0.0, _sf(item.get(amount_key))) for item in original]

    def build(factor: float) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
        out = [dict(item) for item in original]
        deployed = 0.0
        for index, item in enumerate(out):
            ceiling = original_amounts[index] * _clip(factor)
            price = _sf(item.get("portfolio_price") or item.get("price"))
            require_whole = _whole_unit_required(item, whole_units_only, whole_categories)
            item["whole_units_only"] = require_whole
            if require_whole and price > 0:
                units = int(math.floor(ceiling / price + 1e-12))
                amount = round(units * price, 2)
                while units > 0 and amount > ceiling + 0.005:
                    units -= 1
                    amount = round(units * price, 2)
                item[units_key] = float(units)
            else:
                cents = int(math.floor(ceiling * 100.0 + 1e-9))
                amount = cents / 100.0
                item[units_key] = amount / price if price > 0 else 0.0
            if amount > original_amounts[index] + 0.005:
                raise AssertionError("Discrete risk repair increased an allocation")
            item[amount_key] = amount
            deployed += amount
        weighted = [
            (item, _sf(item.get(amount_key)) / budget)
            for item in out
            if _sf(item.get(amount_key)) > EPS
        ]
        signature = risk_signature(weighted)
        zero = deployed <= EPS
        budget_safe = deployed <= budget + 0.005
        safe = zero or (budget_safe and within_risk_target(signature, risk))
        return out, signature, safe

    pre_weighted = [
        (item, original_amounts[index] / budget)
        for index, item in enumerate(original)
        if original_amounts[index] > EPS
    ]
    pre_signature = risk_signature(pre_weighted)
    candidate, candidate_signature, safe = build(1.0)
    if safe:
        return candidate, {
            "risk_scale": 1.0,
            "pre": pre_signature,
            "post": candidate_signature,
            "zero_deployment_safe": sum(original_amounts) <= EPS,
            "adjusted": False,
        }

    # Establish one verified-safe lower bound first.  This remains safe even if
    # whole-lot discontinuities make feasibility locally non-monotonic.
    unsafe_factor = 1.0
    safe_factor = 0.0
    safe_rows, safe_signature, _ = build(0.0)
    probe = 0.5
    for _ in range(60):
        rows, signature, is_safe = build(probe)
        if is_safe:
            safe_factor = probe
            safe_rows = rows
            safe_signature = signature
            break
        unsafe_factor = probe
        probe *= 0.5

    # Refine toward the largest verified-safe common ceiling.  We retain only
    # candidates that individually passed the risk check, so discontinuities
    # cannot turn the returned portfolio unsafe.
    low = safe_factor
    high = unsafe_factor
    for _ in range(60):
        mid = (low + high) / 2.0
        rows, signature, is_safe = build(mid)
        if is_safe:
            low = mid
            safe_factor = mid
            safe_rows = rows
            safe_signature = signature
        else:
            high = mid

    final_deployed = sum(max(0.0, _sf(item.get(amount_key))) for item in safe_rows)
    if final_deployed > budget + 0.005:
        raise AssertionError("Discrete output exceeds budget")
    if final_deployed > EPS and not within_risk_target(safe_signature, risk):
        raise AssertionError("Discrete output exceeds the validated risk contract")
    return safe_rows, {
        "risk_scale": safe_factor,
        "pre": pre_signature,
        "post": safe_signature,
        "zero_deployment_safe": final_deployed <= EPS,
        "adjusted": True,
    }


def production_projection(
    candidates: Iterable[dict[str, Any]],
    weighted: Iterable[tuple[dict[str, Any], float]],
    risk: str,
    budget: float,
    *,
    max_candidate_fraction: float | None = None,
    whole_units_only: bool = False,
    whole_unit_categories: Iterable[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Project validated weights to money/lots without exceeding any ceiling."""
    whole_categories = _whole_unit_category_set(whole_unit_categories)
    candidate_rows = [dict(item) for item in candidates]
    projected, projection = constrained_cent_projection(
        candidate_rows,
        list(weighted),
        risk,
        budget,
    )
    cap_cents = None
    if max_candidate_fraction is not None:
        cap_cents = int(
            math.floor(
                _clip(_sf(max_candidate_fraction), 0.0, 1.0)
                * budget
                * 100.0
                + 1e-9
            )
        )

    whole_unit_residual = 0.0
    for item in projected:
        amount_cents = int(round(max(0.0, _sf(item.get("suggested_amount"))) * 100.0))
        if cap_cents is not None:
            amount_cents = min(amount_cents, cap_cents)
        amount = amount_cents / 100.0
        price = _sf(item.get("portfolio_price") or item.get("price"))
        require_whole = _whole_unit_required(item, whole_units_only, whole_categories)
        if require_whole and price > 0:
            units = int(math.floor(amount / price + 1e-12))
            lot_amount = round(units * price, 2)
            # Floating-price roundoff must never buy a lot above the cent ceiling.
            while units > 0 and lot_amount > amount + 0.005:
                units -= 1
                lot_amount = round(units * price, 2)
            whole_unit_residual += max(0.0, amount - lot_amount)
            amount = lot_amount
            item["suggested_units"] = float(units)
        else:
            item["suggested_units"] = amount / price if price > 0 else 0.0
        item["suggested_amount"] = amount
        item["allocation_weight_validated"] = amount / budget if budget > 0 else 0.0
        item["allocation_eligible"] = amount > 0.0
        item["whole_units_only"] = require_whole

    projected, discrete_risk = enforce_discrete_risk_contract(
        projected,
        risk,
        budget,
        amount_key="suggested_amount",
        units_key="suggested_units",
        whole_units_only=whole_units_only,
        whole_unit_categories=whole_categories,
    )
    for item in projected:
        item["allocation_weight_validated"] = (
            _sf(item.get("suggested_amount")) / budget if budget > 0 else 0.0
        )
        item["allocation_eligible"] = _sf(item.get("suggested_amount")) > 0.0
        item["whole_units_only"] = _whole_unit_required(item, whole_units_only, whole_categories)

    deployed = round(sum(_sf(item.get("suggested_amount")) for item in projected), 2)
    if deployed > budget + 0.005:
        raise AssertionError("Production projection exceeded budget")
    return projected, {
        **projection,
        "post_discrete_risk": discrete_risk,
        "post_risk_projected_total_cents": int(round(deployed * 100.0)),
        "deployed": deployed,
        "cash_reserve": round(max(0.0, budget - deployed), 2),
        "deployment_fraction": deployed / budget if budget > 0 else 0.0,
        "whole_units_only": bool(whole_units_only),
        "whole_unit_categories": sorted(whole_categories),
        "whole_unit_residual": round(whole_unit_residual, 2),
        "candidate_cap_cents": cap_cents,
    }


def clamp_ai_ranking_to_deterministic(
    results: list[dict[str, Any]],
    ranking: Any,
    amount: float | None,
    *,
    risk: str,
    whole_units_only: bool = False,
    whole_unit_categories: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Allow AI to reduce/reorder only; never exceed deterministic ceilings."""
    whole_categories = _whole_unit_category_set(whole_unit_categories)
    valid_actions = {"consider", "watch", "avoid"}
    known = {_identity(item): item for item in results}
    for item in results:
        item["ai_score"] = None
        item["ai_action"] = "watch"
        item["ai_reason"] = ""
        item["ai_suggested_amount"] = None if amount is None else 0.0
        item["ai_suggested_units"] = None if amount is None else 0.0

    has_budget = amount is not None
    budget = max(0.0, _sf(amount)) if has_budget else 0.0
    if isinstance(ranking, list):
        seen: set[tuple[str, str, str]] = set()
        for raw in ranking:
            if not isinstance(raw, dict):
                continue
            identity = _identity(raw)
            item = known.get(identity)
            if item is None or identity in seen:
                continue
            seen.add(identity)
            score = _sf(raw.get("score"), math.nan)
            if math.isfinite(score):
                item["ai_score"] = round(_clip(score, 0.0, 100.0), 2)
            action = str(raw.get("action") or "watch").strip().lower()
            if action == "buy":
                action = "consider"
            action = action if action in valid_actions else "watch"
            ceiling = max(0.0, _sf(item.get("suggested_amount"))) if has_budget else None
            if has_budget and ceiling is not None and ceiling <= EPS and action == "consider":
                action = "watch"
            if not has_budget and not bool(item.get("allocation_eligible", True)) and action == "consider":
                action = "watch"
            item["ai_action"] = action
            item["ai_reason"] = str(raw.get("reason") or "").strip()[:500]
            if not has_budget:
                continue
            proposed = 0.0
            if action == "consider" and ceiling is not None and ceiling > EPS:
                proposed = max(0.0, _sf(raw.get("suggested_amount")))
            suggested = min(proposed, ceiling or 0.0)
            price = _sf(item.get("portfolio_price") or item.get("price"))
            require_whole = _whole_unit_required(item, whole_units_only, whole_categories)
            item["whole_units_only"] = require_whole
            if require_whole and price > 0:
                units = int(math.floor(suggested / price + 1e-12))
                suggested = round(units * price, 2)
                item["ai_suggested_units"] = float(units)
            else:
                item["ai_suggested_units"] = suggested / price if price > 0 else 0.0
            item["ai_suggested_amount"] = suggested

    if not has_budget:
        return {
            "budget": None,
            "deployed": None,
            "cash_reserve": None,
            "validated_deterministic_ceiling": None,
            "whole_units_only": bool(whole_units_only),
            "whole_unit_categories": sorted(whole_categories),
        }

    guarded, ai_risk = enforce_discrete_risk_contract(
        results,
        risk,
        budget,
        amount_key="ai_suggested_amount",
        units_key="ai_suggested_units",
        whole_units_only=whole_units_only,
        whole_unit_categories=whole_categories,
    )
    results[:] = guarded
    for item in results:
        if (
            item.get("ai_action") == "consider"
            and _sf(item.get("ai_suggested_amount")) <= EPS
        ):
            item["ai_action"] = "watch"
    deployed = round(sum(_sf(item.get("ai_suggested_amount")) for item in results), 2)
    deterministic_ceiling = round(
        sum(max(0.0, _sf(item.get("suggested_amount"))) for item in results),
        2,
    )
    if deployed > deterministic_ceiling + 0.005:
        raise AssertionError("AI allocation exceeded deterministic validated ceiling")
    return {
        "budget": round(budget, 2),
        "deployed": deployed,
        "cash_reserve": round(max(0.0, budget - deployed), 2),
        "deployment_fraction": deployed / budget if budget > 0 else 0.0,
        "validated_deterministic_ceiling": deterministic_ceiling,
        "post_ai_risk": ai_risk,
        "whole_units_only": bool(whole_units_only),
        "whole_unit_categories": sorted(whole_categories),
    }
