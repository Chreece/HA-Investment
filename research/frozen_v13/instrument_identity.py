"""Resolve allocation identity from provider evidence and reviewed issuer facts.

This module performs no I/O and never treats a display name, an ISIN alone, or
caller-supplied ``verified`` flags as authority. ``provider_metadata`` must be
built by the provider adapter from its actual response, separately from the
requested candidate. The production manager owns that trust boundary.

Fund classification requires BOTH the exact provider listing and a reviewed
issuer record. The deliberately small catalog covers existing EUR discovery
seeds; it is not a mapping between similar funds or interchangeable histories.
In particular, trading currency is distinct from share-class currency. The
catalog's 180-day review interval is an operational maintenance policy, not an
empirical confidence estimate or a forecast parameter. Expired records abstain
until their issuer information has been reviewed again.
"""
from __future__ import annotations

import datetime as dt
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

IDENTITY_POLICY_VERSION = "exact-provider-and-issuer-listing-v1"
ISSUER_REVIEW_INTERVAL_DAYS = 180
_REVIEWED_ON = dt.date(2026, 10, 6)
_REVIEW_DUE_ON = _REVIEWED_ON + dt.timedelta(days=ISSUER_REVIEW_INTERVAL_DAYS)


@dataclass(frozen=True, slots=True)
class _Listing:
    """One issuer share class on one provider's exact trading listing."""

    provider_id: str
    exchange_ticker: str
    isin: str
    issuer: str
    source_url: str
    share_class: str
    share_class_currency: str
    share_class_inception: str
    economic_sleeve: str
    replication: str = "physical"
    currency_hedged: bool = False
    trading_currency: str = "EUR"
    exchange: str = "XETRA"
    listing_inception: str | None = None


# Issuer pages/factsheets checked on _REVIEWED_ON. Each source lists the exact
# ISIN/share class and the EUR German listing (including its Reuters symbol).
# These are identity facts; the sleeve assignment follows the existing model's
# taxonomy. No price, yield, volatility, score or risk limit lives in this table.
_ISSUER_LISTINGS = MappingProxyType({
    ("yahoo", "XEON.DE"): _Listing(
        "XEON.DE", "XEON", "LU0290358497", "Xtrackers II / DWS",
        "https://etf.dws.com/download/asset/226c6aa2-681e-4c7f-864d-0d717f8718d4",
        "1C", "EUR", "2007-05-25", "cash_like", replication="synthetic_swap",
    ),
    ("yahoo", "CEMK.DE"): _Listing(
        "CEMK.DE", "CEMK", "IE000WV38GP5", "iShares III plc / BlackRock",
        "https://www.ishares.com/uk/individual/en/products/345319/?siteEntryPassthrough=true&switchLocale=y",
        "EUR Accumulating", "EUR", "2025-08-22", "cash_like", listing_inception="2025-08-27",
    ),
    ("yahoo", "VAGF.DE"): _Listing(
        "VAGF.DE", "VAGF", "IE00BG47KH54", "Vanguard Funds plc",
        "https://www.vanguard.co.uk/uk-fund-directory/product/etf/bond/9443/global-aggregate-bond-ucits",
        "EUR Hedged Accumulating", "EUR", "2019-06-18", "aggregate_bond", currency_hedged=True,
        listing_inception="2019-06-20",
    ),
    ("yahoo", "VGGF.DE"): _Listing(
        "VGGF.DE", "VGGF", "IE000B1A2798", "Vanguard Funds plc",
        "https://www.vanguard.co.uk/professional/product/etf/bond/E070/global-government-bond-ucits-etf-eur-hedged-accumulating",
        "EUR Hedged Accumulating", "EUR", "2025-03-25", "government_bond", currency_hedged=True,
        listing_inception="2025-03-27",
    ),
    ("yahoo", "SXR8.DE"): _Listing(
        "SXR8.DE", "SXR8", "IE00B5BMR087", "iShares VII plc / BlackRock",
        "https://www.ishares.com/de/privatanleger/de/produkte/253743/ishares-core-s-p-500-ucits-etf",
        "USD Accumulating", "USD", "2010-05-19", "broad_equity", listing_inception="2010-05-26",
    ),
    ("yahoo", "EUNL.DE"): _Listing(
        "EUNL.DE", "EUNL", "IE00B4L5Y983", "iShares III plc / BlackRock",
        "https://www.ishares.com/de/privatanleger/de/produkte/251882/ishares-msci-world-ucits-etf-acc",
        "USD Accumulating", "USD", "2009-09-25", "broad_equity", listing_inception="2009-10-20",
    ),
    ("yahoo", "VWCE.DE"): _Listing(
        "VWCE.DE", "VWCE", "IE00BK5BQT80", "Vanguard Funds plc",
        "https://www.vanguard.co.uk/professional/product/etf/equity/9679/ftse-all-world-ucits-etf-usd-accumulating",
        "USD Accumulating", "USD", "2019-07-23", "broad_equity",
    ),
})

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
_PLACEHOLDERS = frozenset({"", "UNKNOWN", "NONE", "N A", "YAHOO", "YAHOO FINANCE"})
_CATEGORY_ALIASES = {
    "equity": "stock", "stock": "stock", "etf": "etf", "fund": "fund",
    "mutualfund": "fund", "index": "index", "cryptocurrency": "crypto",
    "crypto": "crypto", "currency": "fx", "fx": "fx", "commodity": "commodity",
    "future": "future", "futures": "future",
}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _words(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _text(value).upper())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^A-Z0-9]+", " ", text).strip()


_EXCHANGE_ALIASES = MappingProxyType({
    _words(alias): _words(group[0]) for group in _EXCHANGE_GROUPS for alias in group
})


def _exchange(value: Any) -> str:
    words = _words(value)
    return "" if words in _PLACEHOLDERS else _EXCHANGE_ALIASES.get(words, words)


def provider_exchanges_match(left: Any, right: Any) -> bool:
    """Compare observed venue identifiers through explicit aliases only."""
    first, second = _exchange(left), _exchange(right)
    return bool(first and second and first == second)


def _currency(value: Any) -> str:
    raw = _text(value)
    # GBp and GBX both mean one penny; neither is a GBP unit. Never use the
    # search UI's broader same-portfolio-currency comparison for identity.
    if raw == "GBp" or raw.upper() == "GBX":
        return "GBX"
    raw = raw.upper()
    return raw if re.fullmatch(r"[A-Z]{3,8}", raw) and raw not in _PLACEHOLDERS else ""


def _category(value: Any) -> str:
    return _CATEGORY_ALIASES.get(_text(value).lower().replace(" ", ""), "other")


def _symbol(value: Any, category: str) -> str:
    symbol = _text(value).upper()
    if category == "crypto":
        symbol = symbol.replace("-", "/")
        # Explicit display aliases only: no stripping arbitrary X/Z prefixes.
        aliases = {"XBT": "BTC", "XDG": "DOGE"}
        symbol = "/".join(aliases.get(part, part) for part in symbol.split("/"))
    return symbol


def _valid_isin(value: str) -> bool:
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", value):
        return False
    digits = "".join(str(ord(char) - 55) if char.isalpha() else char for char in value)
    total = 0
    for position, digit in enumerate(reversed(digits)):
        number = int(digit) * (2 if position % 2 else 1)
        total += number // 10 + number % 10
    return total % 10 == 0


def _as_date(value: str | dt.date | None) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            pass
    return None


def _structure_flags(row: Mapping[str, Any]) -> list[str]:
    """Name/field warnings can only reject, never approve a structure."""
    name = _words(row.get("name"))
    raw_name = _text(row.get("name")).upper().replace("×", "X")
    flags: list[str] = []
    leveraged = bool(re.search(r"\b(?:LEVERAGED?|INVERSE|ULTRAPRO|ULTRASHORT|BEAR)\b", name))
    leveraged |= bool(re.search(r"(?<![A-Z0-9])[-+]?[1-9](?:[.,][0-9]+)?\s*X\b", raw_name))
    if row.get("inverse") is True or row.get("leveraged") is True:
        leveraged = True
    if "leverage_factor" in row and row["leverage_factor"] is not None:
        try:
            factor = float(row["leverage_factor"])
            leveraged |= isinstance(row["leverage_factor"], bool) or not math.isfinite(factor) or factor != 1.0
        except (TypeError, ValueError, OverflowError):
            leveraged = True
    if leveraged:
        flags.append("leveraged_or_inverse_structure")
    if re.search(r"\b(?:HIGH YIELD|JUNK|BELOW INVESTMENT GRADE|SUB INVESTMENT GRADE)\b", name) or row.get("high_yield") is True:
        flags.append("high_yield_credit_structure")
    # A maturity description is not evidence of a short-selling strategy.
    without_maturity = re.sub(r"\b(?:ULTRA )?SHORT (?:TERM|DURATION|MATURITY)\b", "", name)
    if re.search(r"\bSHORT\b", without_maturity):
        flags.append("short_strategy_structure_unverified")
    if re.search(r"\b(?:COVERED CALL|LONG SHORT|MANAGED FUTURES|DEFINED OUTCOME|BUFFERED|OPTION INCOME)\b", name):
        flags.append("other_complex_fund_structure")
    return flags


def _catalog_assertion_blockers(row: Mapping[str, Any], listing: _Listing) -> list[str]:
    """Reject contradictory identifiers/structure claims without name matching."""
    blockers: list[str] = []
    isin = _text(row.get("isin")).upper()
    if isin and isin != listing.isin:
        blockers.append("isin_mismatch")
    share_currency = _currency(row.get("share_class_currency"))
    if row.get("share_class_currency") not in (None, "") and share_currency != listing.share_class_currency:
        blockers.append("share_class_currency_mismatch")

    share_class = _words(row.get("share_class"))
    if row.get("share_class") not in (None, ""):
        aliases = {_words(listing.share_class)}
        if listing.share_class == "1C":
            aliases.add("EUR 1C")
        else:
            aliases |= {"ACC", "ACCUMULATING", "ACCUMULATION"}
            aliases.add(_words(listing.share_class).replace("ACCUMULATING", "ACC"))
        if share_class not in aliases:
            blockers.append("share_class_mismatch")

    # Names remain display text; only explicit contradictory assertions matter.
    name = _words(row.get("name"))
    if re.search(r"\b(?:DIST|DISTRIBUTING|DISTRIBUTION|AUSSCHUTTEND)\b", name):
        blockers.append("income_treatment_mismatch")
    income = _words(row.get("income_treatment"))
    if income and income not in {"ACC", "ACCUMULATING", "ACCUMULATION", "THESAURIEREND"}:
        blockers.append("income_treatment_mismatch")
    if row.get("currency_hedged") is not None and row.get("currency_hedged") is not listing.currency_hedged:
        blockers.append("currency_hedging_mismatch")
    if "UNHEDGED" in name.split() and listing.currency_hedged:
        blockers.append("currency_hedging_mismatch")
    if "HEDGED" in name.split() and not listing.currency_hedged:
        blockers.append("currency_hedging_mismatch")
    hedge_currency = re.search(r"\b(EUR|USD|GBP|CHF|JPY|AUD|CAD) HEDGED\b", name)
    if hedge_currency and hedge_currency.group(1) != listing.share_class_currency:
        blockers.append("currency_hedging_mismatch")
    if re.search(r"\b(?:SWAP|SYNTHETIC)\b", name) and listing.replication != "synthetic_swap":
        blockers.append("replication_structure_mismatch")
    if "PHYSICAL" in name.split() and listing.replication != "physical":
        blockers.append("replication_structure_mismatch")
    if row.get("replication") not in (None, ""):
        replication_aliases = {
            "physical": {"PHYSICAL", "PHYSICAL REPLICATION"},
            "synthetic_swap": {"SYNTHETIC SWAP", "SYNTHETIC", "SWAP"},
        }
        if _words(row.get("replication")) not in replication_aliases[listing.replication]:
            blockers.append("replication_structure_mismatch")
    return blockers


def resolve_instrument_identity(
    asset: Mapping[str, Any],
    *,
    provider_metadata: Mapping[str, Any] | None = None,
    as_of: str | dt.date | None = None,
) -> dict[str, Any]:
    """Return a fresh, JSON-safe identity/classification decision.

    ``asset`` is a requested listing, not authoritative evidence. The separate
    metadata argument must carry ``provider``, actual returned ``symbol``,
    ``category``, native quote-unit ``currency`` and ``exchange``. For providers
    other than Yahoo it also needs the actual returned ``provider_id``: those
    IDs need not be display symbols. Yahoo's returned symbol is its listing ID.

    ``as_of`` is deliberately explicit so offline replays are deterministic.
    Omitting it abstains for issuer-catalog funds. No field named ``verified``,
    ``instrument_identity`` or ``economic_sleeve`` is read from either input.
    A positive decision establishes product identity only; history, freshness,
    suitability and portfolio-risk gates remain the caller's responsibility.
    """
    result: dict[str, Any] = {
        "instrument_identity_policy": IDENTITY_POLICY_VERSION,
        "instrument_identity_status": "unverified",
        "instrument_identity_eligible": False,
        "instrument_identity_blockers": [],
        "economic_subtype": "unknown",
        "economic_sleeve": "unknown",
        "allocatable": False,
        "economic_classification_confidence": None,
        "economic_classification_basis": "unverified_product_identity",
        "product_structure_flags": [],
        "issuer_name": None,
        "issuer_source_url": None,
        "issuer_reviewed_on": None,
        "issuer_review_due_on": None,
        "isin": None,
        "share_class": None,
        "share_class_currency": None,
        "share_class_inception": None,
        "listing_inception": None,
        "listing_currency": None,
        "listing_exchange": None,
    }
    if not isinstance(asset, Mapping):
        result["instrument_identity_blockers"] = ["invalid_candidate_identity"]
        return result
    metadata = provider_metadata if isinstance(provider_metadata, Mapping) else {}
    provider = _text(asset.get("provider")).lower()
    requested_id = _text(asset.get("provider_id"))
    category = _category(asset.get("category"))
    requested_symbol = _symbol(asset.get("symbol") or requested_id, category)
    listing = _ISSUER_LISTINGS.get((provider, requested_id.upper()))
    blockers: list[str] = []

    # References cannot turn into assets merely by changing a name or evidence.
    if category in {"index", "fx", "future"} or requested_symbol.startswith("^") or requested_symbol.endswith(("=F", "=X")):
        subtype = "benchmark" if category == "index" or requested_symbol.startswith("^") else "currency" if category == "fx" or requested_symbol.endswith("=X") else "commodity_future"
        result.update(economic_subtype=subtype, economic_sleeve="nontradable", economic_classification_basis="nontradable_reference")
        result["instrument_identity_blockers"] = ["nontradable_reference"]
        return result

    flags: list[str] = []
    if category in {"etf", "fund"} or listing is not None:
        flags = list(dict.fromkeys([*_structure_flags(asset), *_structure_flags(metadata)]))
        blockers.extend(f"unsupported_{flag}" for flag in flags)
    result["product_structure_flags"] = flags
    if not provider or not requested_id:
        blockers.append("requested_provider_identity_missing")
    if not metadata:
        blockers.append("provider_metadata_missing")
    if _text(metadata.get("provider")).lower() != provider or not provider:
        blockers.append("provider_source_mismatch" if metadata.get("provider") else "provider_source_missing")
    observed_category = _category(metadata.get("category"))
    if observed_category == "other":
        blockers.append("provider_product_type_missing")
    elif observed_category != category:
        blockers.append("provider_product_type_mismatch")
    observed_symbol = _symbol(metadata.get("symbol"), category)
    observed_id = _text(metadata.get("provider_id"))
    if not observed_symbol:
        blockers.append("provider_symbol_missing")
    if provider == "yahoo":
        if observed_symbol and observed_symbol != _symbol(requested_id, category):
            blockers.append("provider_listing_mismatch")
        if observed_id and observed_id.upper() != requested_id.upper():
            blockers.append("provider_listing_mismatch")
        requested_aliases = {_symbol(requested_id, category)}
        if listing is not None:
            requested_aliases.add(listing.exchange_ticker)
        if requested_symbol not in requested_aliases:
            blockers.append("requested_symbol_mismatch")
    else:
        if not observed_id:
            blockers.append("provider_listing_id_missing")
        elif observed_id != requested_id:
            blockers.append("provider_listing_mismatch")
        if observed_symbol and observed_symbol != requested_symbol:
            blockers.append("provider_symbol_mismatch")

    observed_currency = _currency(metadata.get("currency"))
    requested_currency = _currency(asset.get("currency"))
    if not observed_currency:
        blockers.append("provider_currency_missing")
    if asset.get("currency") not in (None, "") and requested_currency != observed_currency:
        blockers.append("quote_currency_mismatch")
    observed_exchange = _exchange(metadata.get("exchange"))
    requested_exchange = _exchange(asset.get("exchange"))
    if not observed_exchange:
        blockers.append("provider_exchange_missing")
    if requested_exchange and requested_exchange != observed_exchange:
        blockers.append("exchange_mismatch")

    for row in (asset, metadata):
        if row.get("isin") not in (None, "") and not _valid_isin(_text(row.get("isin")).upper()):
            blockers.append("invalid_isin")

    if listing is not None:
        result.update(
            issuer_name=listing.issuer, issuer_source_url=listing.source_url,
            issuer_reviewed_on=_REVIEWED_ON.isoformat(), issuer_review_due_on=_REVIEW_DUE_ON.isoformat(),
        )
        if category != "etf" or observed_category != "etf":
            blockers.append("catalog_product_type_mismatch")
        if observed_currency != listing.trading_currency:
            blockers.append("catalog_listing_currency_mismatch")
        if observed_exchange != listing.exchange:
            blockers.append("catalog_listing_exchange_mismatch")
        for row in (asset, metadata):
            blockers.extend(_catalog_assertion_blockers(row, listing))
        review_date = _as_date(as_of)
        if review_date is None:
            blockers.append("catalog_review_date_missing" if as_of is None else "catalog_review_date_invalid")
        elif review_date < _REVIEWED_ON:
            blockers.append("catalog_not_reviewed_as_of_date")
        elif review_date > _REVIEW_DUE_ON:
            blockers.append("issuer_catalog_review_expired")
    elif category in {"etf", "fund"}:
        blockers.append("issuer_fund_identity_unverified")
    elif category not in {"stock", "crypto"}:
        blockers.append("product_structure_unverified")
    else:
        requested_isin = _text(asset.get("isin")).upper()
        observed_isin = _text(metadata.get("isin")).upper()
        if requested_isin and not observed_isin:
            blockers.append("provider_isin_missing")
        elif requested_isin and requested_isin != observed_isin:
            blockers.append("isin_mismatch")
        for field in ("share_class", "share_class_currency"):
            if _text(asset.get(field)) and _words(asset.get(field)) != _words(metadata.get(field)):
                blockers.append(f"{field}_mismatch")
        if category == "crypto":
            parts = observed_symbol.split("/")
            if len(parts) != 2 or not all(parts) or parts[1] != observed_currency:
                blockers.append("crypto_quote_pair_mismatch")

    blockers = list(dict.fromkeys(blockers))
    if blockers:
        result["instrument_identity_blockers"] = blockers
        if any("mismatch" in reason or reason == "invalid_isin" for reason in blockers):
            result["instrument_identity_status"] = "conflicting"
        elif "issuer_catalog_review_expired" in blockers:
            result["instrument_identity_status"] = "expired_catalog"
        if flags:
            result["economic_classification_basis"] = "unsupported_product_structure"
        return result

    result.update(instrument_identity_eligible=True, allocatable=True,
                  listing_currency=observed_currency, listing_exchange=observed_exchange)
    if listing is not None:
        result.update(
            instrument_identity_status="issuer_catalog_and_provider_listing",
            economic_classification_basis="issuer_catalog_exact_listing",
            economic_subtype=listing.economic_sleeve, economic_sleeve=listing.economic_sleeve,
            isin=listing.isin, share_class=listing.share_class,
            share_class_currency=listing.share_class_currency,
            share_class_inception=listing.share_class_inception,
            listing_inception=listing.listing_inception,
        )
        if listing.replication == "synthetic_swap":
            result["product_structure_flags"].append("swap_based_structure")
        if listing.currency_hedged:
            result["product_structure_flags"].append("currency_hedged_structure")
    else:
        sleeve = "single_equity" if category == "stock" else "crypto"
        result.update(
            instrument_identity_status="provider_listing",
            economic_classification_basis="provider_listing_product_type",
            economic_subtype=sleeve, economic_sleeve=sleeve,
            isin=_text(metadata.get("isin")).upper() or None,
        )
    return result
