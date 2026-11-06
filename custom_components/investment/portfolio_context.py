"""Pure portfolio-context helpers used by the alternate runtime layer."""
from __future__ import annotations

import math
import re
from typing import Any, Iterable

_INSTRUMENT_ID_FIELDS = ("share_class_figi", "isin")
_ID_FIELDS = (*_INSTRUMENT_ID_FIELDS, "figi")
_TOKEN_PRIORITY = {key: position for position, key in enumerate((*_ID_FIELDS, "provider", "listing", "symbol"))}
_WORD_RE = re.compile(r"[^A-Z0-9]+")
# Explicit venue aliases, following the provider-identity boundary.  Similar
# venue names are never sufficient evidence of identical listings.
_EXCHANGE_GROUPS = (
    ("XETRA", "XETR", "GER", "DEUTSCHE BORSE", "DEUTSCHE BORSE AG", "DEUTSCHE BOERSE", "DEUTSCHE BOERSE AG"),
    ("NASDAQ", "NAS", "NMS", "NASDAQGS", "NASDAQ GLOBAL SELECT MARKET"),
    ("NASDAQGM", "NGM", "NASDAQ GLOBAL MARKET"),
    ("NASDAQCM", "NCM", "NASDAQ CAPITAL MARKET"),
    ("NYSE", "NYQ", "XNYS", "NEW YORK STOCK EXCHANGE"),
    ("NYSE ARCA", "NYSEARCA", "PCX", "ARCX", "ARCA"),
    ("LSE", "LONDON", "LONDON STOCK EXCHANGE", "XLON"),
    ("EURONEXT AMSTERDAM", "AMSTERDAM", "AMS", "XAMS"),
    ("EURONEXT PARIS", "PARIS", "PAR", "XPAR"),
    ("KRAKEN",),
    ("CCC", "CCC CRYPTO"),
)
_EXCHANGE_ALIASES = {
    alias: group[0] for group in _EXCHANGE_GROUPS for alias in group
}
_UNKNOWN_EXCHANGES = {"", "UNKNOWN", "NONE", "N A", "YAHOO", "YAHOO FINANCE"}
# These are the explicit market suffixes supported by providers/yahoo.py.
# A dot by itself is not a venue qualifier: BRK.B is a share-class ticker.
_LISTING_SUFFIXES = frozenset({
    "AE", "BA", "AX", "XA", "SA", "TO", "V", "CN", "NE", "SW",
    "SN", "SS", "SZ", "CL", "PR", "CO", "CA", "DE", "AS", "PA",
    "MI", "MC", "BR", "VI", "HE", "AT", "IR", "LS", "F", "TI",
    "TL", "RG", "VS", "L", "IL", "AQ", "XC", "HK", "BD", "JK",
    "TA", "NS", "BO", "IC", "T", "KS", "KQ", "KW", "MX", "KL",
    "OL", "NZ", "PS", "WA", "QA", "RO", "SAU", "ST", "SI", "BK",
    "IS", "TW", "TWO", "VN", "JO",
})

OwnedIdentityIndex = dict[str, list[dict[str, Any]]]


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _symbol(value: Any, category: str | None = None) -> str:
    text = _clean(value).upper()
    if str(category or "").lower() in {"crypto", "fx"}:
        text = text.replace("-", "/").replace("=X", "")
    return text


def _exchange(value: Any) -> str:
    name = _WORD_RE.sub(" ", _clean(value).upper()).strip()
    return "" if name in _UNKNOWN_EXCHANGES else _EXCHANGE_ALIASES.get(name, name)


def _quote_currency(asset: dict[str, Any]) -> str:
    value = _clean(asset.get("quote_currency") or asset.get("currency"))
    # A penny quote is not a pound quote.  Case folding must preserve GBp.
    return "GBX" if value == "GBp" or value.upper() == "GBX" else value.upper()


def instrument_identity_tokens(asset: dict[str, Any]) -> set[str]:
    """Return lookup aliases, which are verified before accepting an identity.

    A shared symbol is a lookup key, not proof of ownership.  The matcher checks
    stronger assertions and listing context before using any fallback alias.
    Names deliberately never become aliases.
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
        exchange = _exchange(asset.get("exchange"))
        if exchange:
            tokens.add(f"listing:{category}:{symbol}:{exchange}")
    return tokens


def build_owned_identity_index(holdings: Iterable[dict[str, Any]]) -> OwnedIdentityIndex:
    """Index currently held positive-quantity instruments by conservative identity."""
    index: OwnedIdentityIndex = {}
    for holding in holdings:
        if not isinstance(holding, dict) or isinstance(holding.get("quantity"), bool):
            continue
        try:
            quantity = float(holding.get("quantity") or 0.0)
        except (TypeError, ValueError, OverflowError):
            quantity = 0.0
        if not math.isfinite(quantity) or quantity <= 1e-12:
            continue
        for token in instrument_identity_tokens(holding):
            # An ambiguous alias must not discard a later, compatible holding.
            index.setdefault(token, []).append(holding)
    return index


def _token_order(token: str) -> tuple[int, str]:
    kind = token.partition(":")[0]
    return _TOKEN_PRIORITY.get(kind, len(_TOKEN_PRIORITY)), token


def _holding_order(asset: dict[str, Any]) -> tuple[str, ...]:
    return tuple(_clean(asset.get(key)).upper() for key in (
        *_ID_FIELDS, "provider", "provider_id", "symbol", "exchange", "currency", "id",
    ))


def _identity_alias_relation(
    asset: dict[str, Any], holding: dict[str, Any], token: str,
) -> str:
    """Distinguish positive identity, explicit conflict, and missing evidence."""
    for key in _INSTRUMENT_ID_FIELDS:
        left, right = _clean(asset.get(key)).upper(), _clean(holding.get(key)).upper()
        if left and right and left != right:
            return "conflicting"

    kind = token.partition(":")[0]
    if kind in _INSTRUMENT_ID_FIELDS:
        # The same share class can trade on more than one venue or in more than
        # one quote currency and have a different venue-level FIGI.  Those
        # listing differences do not negate an agreed instrument/share-class
        # ID, but contradictory instrument/share-class assertions do.
        return "matched"
    figi, held_figi = _clean(asset.get("figi")).upper(), _clean(holding.get("figi")).upper()
    if figi and held_figi and figi != held_figi:
        return "conflicting"
    if kind == "figi":
        return "matched"

    category = _clean(asset.get("category") or "other").lower()
    held_category = _clean(holding.get("category") or "other").lower()
    if category != "other" and held_category != "other" and category != held_category:
        return "conflicting"
    symbol = _symbol(asset.get("symbol"), category)
    held_symbol = _symbol(holding.get("symbol"), held_category)
    if symbol and held_symbol and symbol != held_symbol:
        return "conflicting"
    exchange, held_exchange = _exchange(asset.get("exchange")), _exchange(holding.get("exchange"))
    if exchange and held_exchange and exchange != held_exchange:
        return "conflicting"
    currency, held_currency = _quote_currency(asset), _quote_currency(holding)
    if currency and held_currency and currency != held_currency:
        return "conflicting"

    if kind in {"provider", "listing"}:
        return "matched"
    if kind != "symbol":
        return "conflicting"
    if exchange and held_exchange:
        return "matched"
    # Bare ticker symbols are not unique across venues/providers.  Retain the
    # supported cross-provider qualified-listing fallback (e.g. EUNL.DE).
    qualified = _symbol(asset.get("symbol") or asset.get("provider_id"), category)
    root, separator, suffix = qualified.rpartition(".")
    if category in {"stock", "etf", "fund"} and root and separator and suffix in _LISTING_SUFFIXES:
        return "matched"
    # A shared bare ticker without its venue cannot certify either sameness or
    # difference. Exact-match callers still reject it; ownership exclusion also
    # needs to know that the rejection did not prove candidate absence.
    return "ambiguous"


def _find_identity_relation(
    asset: dict[str, Any],
    owned_index: OwnedIdentityIndex | dict[str, dict[str, Any]] | None,
    relation: str,
) -> dict[str, Any] | None:
    if not owned_index:
        return None
    for token in sorted(instrument_identity_tokens(asset), key=_token_order):
        matches = owned_index.get(token) or []
        # Accept a legacy single-row index for callers upgrading independently.
        if isinstance(matches, dict):
            matches = [matches]
        for match in sorted(matches, key=_holding_order):
            if _identity_alias_relation(asset, match, token) == relation:
                return match
    return None


def find_owned_match(
    asset: dict[str, Any],
    owned_index: OwnedIdentityIndex | dict[str, dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Match exact identity in deterministic order, never contradicting IDs."""
    return _find_identity_relation(asset, owned_index, "matched")


def find_ambiguous_identity_match(
    asset: dict[str, Any],
    owned_index: OwnedIdentityIndex | dict[str, dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Find a shared alias lacking enough evidence to establish either outcome."""
    return _find_identity_relation(asset, owned_index, "ambiguous")


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
    # V14b already carries the V10/V13 risk-invariant market score and moves
    # portfolio context to a downward-only allocation constraint.  Re-running
    # the legacy generic restoration here would overwrite the defensive-sleeve
    # score (cash/government/aggregate bonds) after allocation had completed.
    if metrics.get("market_score_risk_profile_invariant") is True:
        try:
            market_score = float(item.get("market_score", item.get("score")))
        except (TypeError, ValueError):
            market_score = math.nan
        if math.isfinite(market_score):
            return {
                "market_score": round(max(0.0, min(100.0, market_score)), 2),
                "portfolio_context_adjustment": 0.0,
            }
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
