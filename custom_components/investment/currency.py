"""Currency identity/scaling helpers for HA Investment.

Portfolio/reporting currencies are ISO-4217 codes. A few market feeds also use
quote-unit labels such as ``GBp``/``GBX`` for pence sterling; those labels must
remain distinct from GBP while displaying/storing native market prices.
"""
from __future__ import annotations


def canonical_currency(currency: str | None, default: str = "EUR") -> str:
    """Return a stable currency/quote-unit identifier without losing GBp case."""
    raw = str(currency or default).strip()
    if raw == "GBp":
        return "GBp"
    if raw.upper() == "GBX":
        return "GBX"
    return raw.upper()


def normalize_currency(currency: str | None, default: str = "EUR") -> tuple[str, float]:
    """Return ISO currency plus multiplier from the native quote unit.

    One GBp/GBX unit is one penny, i.e. 0.01 GBP. All ordinary ISO currencies
    have scale 1.0.
    """
    value = canonical_currency(currency, default)
    if value in {"GBp", "GBX"}:
        return "GBP", 0.01
    return value, 1.0


def default_settlement_currency(trade_currency: str | None, default: str = "EUR") -> str:
    """Return the natural cash settlement currency for one trading quote."""
    value = canonical_currency(trade_currency, default)
    return "GBP" if value in {"GBp", "GBX"} else value


def fixed_conversion_rate(
    source_currency: str | None, target_currency: str | None, default: str = "EUR"
) -> float | None:
    """Return a deterministic same-currency/quote-unit conversion when known.

    This prevents identity legs (EUR->EUR) and fixed quote-unit scaling
    (GBp/GBX->GBP) from being overridden by arbitrary user input.
    """
    source, source_scale = normalize_currency(source_currency, default)
    target, target_scale = normalize_currency(target_currency, default)
    if source != target:
        return None
    return source_scale / target_scale


def frozen_transaction_rate(
    transaction: dict,
    source_currency: str | None,
    target_currency: str | None,
) -> float | None:
    """Return a conversion fully determined by a stored transaction, if possible.

    ``trade_fx_rate`` is native trade currency -> settlement currency and
    ``fx_rate`` is settlement currency -> portfolio currency at the transaction.
    This helper deliberately does not fetch market data; callers can fall back to
    historical FX only when the frozen transaction legs cannot answer the pair.
    """
    source = canonical_currency(source_currency)
    target = canonical_currency(target_currency)
    trade = canonical_currency(transaction.get("transaction_currency") or source)
    settlement = canonical_currency(
        transaction.get("settlement_currency") or transaction.get("fee_currency") or trade
    )
    portfolio = canonical_currency(
        transaction.get("portfolio_currency_at_transaction") or target
    )

    if source == target:
        return 1.0

    trade_fx = transaction.get("trade_fx_rate")
    settlement_fx = transaction.get("fx_rate")
    quote_fx = transaction.get("quote_fx_rate")

    if source == settlement and target == trade and trade_fx not in (None, 0):
        return 1.0 / float(trade_fx)
    if source == settlement and target == portfolio and settlement_fx is not None:
        return float(settlement_fx)
    if source == trade and target == settlement and trade_fx is not None:
        return float(trade_fx)
    if source == trade and target == portfolio:
        if trade_fx is not None and settlement_fx is not None:
            return float(trade_fx) * float(settlement_fx)
        if quote_fx is not None:
            return float(quote_fx)
    return None
