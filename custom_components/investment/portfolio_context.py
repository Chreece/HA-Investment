"""Pure portfolio-context helpers used by the alternate runtime layer."""
from __future__ import annotations

import math
import re
from typing import Any, Iterable

_ID_FIELDS = ("share_class_figi", "figi", "isin")
_WORD_RE = re.compile(r"[^A-Z0-9]+")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _symbol(value: Any, category: str | None = None) -> str:
    text = _clean(value).upper()
    if str(category or "").lower() in {"crypto", "fx"}:
        text = text.replace("-", "/").replace("=X", "")
    return text


def instrument_identity_tokens(asset: dict[str, Any]) -> set[str]:
    """Return conservative identity tokens for a tradable instrument.

    Strong identifiers win when available.  Provider IDs and exact listing
    symbols are retained as deterministic fallbacks; no fuzzy name matching is
    used because similarly named share classes can have different economics.
    """
    tokens: set[str] = set()
    category = _clean(asset.get("category") or "other").lower()
    for key in _ID_FIELDS:
        value = _clean(asset.get(key)).upper()
        if value:
            tokens.add(f"{key}:{value}")

    provider = _clean(asset.get("provider")).lower()
    provider_id = _clean(asset.get("provider_id")).upper()
    if provider and provider_id:
        tokens.add(f"provider:{provider}:{provider_id}")

    symbol = _symbol(asset.get("symbol") or provider_id, category)
    if symbol:
        tokens.add(f"symbol:{category}:{symbol}")
        exchange = _clean(asset.get("exchange")).upper()
        if exchange:
            tokens.add(f"listing:{category}:{symbol}:{exchange}")
    return tokens


def build_owned_identity_index(holdings: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index currently held positive-quantity instruments by conservative identity."""
    index: dict[str, dict[str, Any]] = {}
    for holding in holdings:
        try:
            quantity = float(holding.get("quantity") or 0.0)
        except (TypeError, ValueError):
            quantity = 0.0
        if not math.isfinite(quantity) or quantity <= 1e-12:
            continue
        for token in instrument_identity_tokens(holding):
            index.setdefault(token, holding)
    return index


def find_owned_match(asset: dict[str, Any], owned_index: dict[str, dict[str, Any]] | None) -> dict[str, Any] | None:
    """Return the first current holding that is the same identified instrument."""
    if not owned_index:
        return None
    for token in instrument_identity_tokens(asset):
        match = owned_index.get(token)
        if match is not None:
            return match
    return None


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def exposure_profile(asset: dict[str, Any]) -> dict[str, Any]:
    """Classify economic exposure separately from the product wrapper."""
    category = _clean(asset.get("category") or "other").lower()
    symbol = _symbol(asset.get("symbol") or asset.get("provider_id"), category)
    name = _WORD_RE.sub(" ", _clean(asset.get("name")).upper()).strip()

    vehicle = category
    exposure = "other"
    region = "global"
    allocatable = True

    if category == "index":
        vehicle, exposure, allocatable = "benchmark", "benchmark", False
    elif category == "commodity" and symbol.endswith("=F"):
        vehicle, exposure, allocatable = "future", "commodity_future", False
    elif category == "crypto":
        exposure = "crypto"
    elif category == "fx":
        exposure = "currency"
    elif category == "stock":
        exposure = "single_equity"
    elif category == "commodity":
        exposure = "commodity"
    elif category in {"etf", "fund"}:
        vehicle = category
        if _contains_any(name, ("OVERNIGHT", "MONEY MARKET", "ULTRASHORT", "ULTRA SHORT", "0 1YR", "0 1 YEAR", "ESTR", "STR DAILY")):
            exposure = "cash_like"
            region = "eurozone" if _contains_any(name, ("EUR", "EURO")) else "global"
        elif _contains_any(name, ("GOVT BOND", "GOVERNMENT BOND", "TREASURY")):
            exposure = "government_bond"
        elif "BOND" in name:
            exposure = "aggregate_bond"
        elif _contains_any(name, ("S P 500", "NASDAQ 100", "NASDAQ", "US EQUITY", "USA")):
            exposure, region = "broad_equity", "us"
        elif _contains_any(name, ("ALL WORLD", "ALL WORLD", "ACWI", "GLOBAL EQUITY")):
            exposure, region = "broad_equity", "global"
        elif "MSCI WORLD" in name or "WORLD" in name:
            exposure, region = "broad_equity", "developed"
        elif "EMERGING" in name:
            exposure, region = "broad_equity", "emerging"
        elif _contains_any(name, ("TECHNOLOGY", " TECH ", "SEMICONDUCTOR")):
            exposure = "sector_equity"
        elif _contains_any(name, ("GOLD", "SILVER", "COMMODITY")):
            exposure = "commodity"
        else:
            exposure = "fund_other"

    return {
        "vehicle_type": vehicle,
        "exposure_class": exposure,
        "exposure_region": region,
        "allocatable_default": allocatable,
    }


def context_score_fields(item: dict[str, Any]) -> dict[str, float | None]:
    """Recover the standalone market score from deterministic context penalties."""
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    try:
        final_score = float(item.get("score"))
        confidence = float(item.get("confidence") or 0.0)
        signal = float(metrics.get("signal_score_before_suitability", final_score))
        concentration = float(metrics.get("concentration_penalty") or 0.0)
        overlap_penalty = float(metrics.get("overlap_score_penalty") or 0.0)
    except (TypeError, ValueError):
        return {"market_score": None, "portfolio_context_adjustment": None}

    if not all(math.isfinite(value) for value in (final_score, confidence, signal, concentration, overlap_penalty)):
        return {"market_score": None, "portfolio_context_adjustment": None}

    restored = signal + confidence * (12.0 * max(0.0, concentration) + max(0.0, overlap_penalty))
    restored = max(0.0, min(100.0, restored))
    if metrics.get("suitability_eligible") is False:
        restored = min(restored, 47.0)
    market_score = round(restored, 2)
    return {
        "market_score": market_score,
        "portfolio_context_adjustment": round(final_score - market_score, 2),
    }
