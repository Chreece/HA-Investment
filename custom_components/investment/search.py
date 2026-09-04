"""Pure search metadata shared by free market-data providers."""
from __future__ import annotations

CRYPTO_NAMES = {
    "BTC": ("Bitcoin", "bitcoin", "xbt"),
    "BCH": ("Bitcoin Cash", "bitcoin cash", "bcash"),
    "BTG": ("Bitcoin Gold", "bitcoin gold", "btg"),
    "ETH": ("Ethereum", "ethereum", "ether"),
    "ETC": ("Ethereum Classic", "ethereum classic"),
    "LTC": ("Litecoin", "litecoin"),
    "XRP": ("XRP", "ripple", "xrp"),
    "DOGE": ("Dogecoin", "dogecoin", "doge"),
    "ADA": ("Cardano", "cardano"),
    "SOL": ("Solana", "solana"),
    "DOT": ("Polkadot", "polkadot"),
    "LINK": ("Chainlink", "chainlink"),
    "AVAX": ("Avalanche", "avalanche"),
    "UNI": ("Uniswap", "uniswap"),
    "ATOM": ("Cosmos", "cosmos"),
    "XLM": ("Stellar", "stellar"),
    "TRX": ("TRON", "tron"),
    "ALGO": ("Algorand", "algorand"),
    "FIL": ("Filecoin", "filecoin"),
    "AAVE": ("Aave", "aave"),
    "MATIC": ("Polygon", "polygon", "matic"),
    "POL": ("Polygon", "polygon", "pol"),
    "SHIB": ("Shiba Inu", "shiba inu", "shib"),
    "DAI": ("Dai", "dai"),
    "USDT": ("Tether", "tether", "usdt"),
    "USDC": ("USD Coin", "usd coin", "usdc"),
}

def crypto_name(symbol: str) -> str:
    values = CRYPTO_NAMES.get(symbol.upper())
    return values[0] if values else symbol.upper()

def crypto_aliases(symbol: str) -> tuple[str, ...]:
    return CRYPTO_NAMES.get(symbol.upper(), ())


def target_crypto_symbol(symbol: str, quote_currency: str) -> str | None:
    """Return a Yahoo-style crypto symbol in the requested quote currency."""
    upper = str(symbol or "").upper()
    if "-" not in upper:
        return None
    base = upper.rsplit("-", 1)[0].strip()
    quote = str(quote_currency or "").upper().strip()
    if not base or len(quote) != 3:
        return None
    return f"{base}-{quote}"


def crypto_query_score(query: str, base: str, *identifiers: str) -> int | None:
    """Score a crypto query against ticker, IDs and human-name aliases."""
    q = str(query or "").strip().lower()
    if not q:
        return None
    aliases = crypto_aliases(base)
    exact = {str(base).lower(), *(str(value).lower() for value in identifiers if value), *(a.lower() for a in aliases)}
    haystack = " ".join([str(base), *identifiers, *aliases]).lower()
    if q not in haystack:
        return None
    return 0 if q in exact else 1


def crypto_symbol_from_query(query: str) -> str | None:
    """Resolve a ticker/name query to a known crypto symbol when unambiguous."""
    q = str(query or "").strip().lower()
    if not q:
        return None
    for symbol, aliases in CRYPTO_NAMES.items():
        if q == symbol.lower() or q in {str(alias).lower() for alias in aliases}:
            return symbol
    # A short all-alpha token may itself be a crypto ticker. Yahoo chart
    # metadata is used by the caller to verify it is really cryptocurrency.
    raw = str(query or "").strip().upper()
    if 2 <= len(raw) <= 10 and raw.isalpha():
        return raw
    return None


def yahoo_crypto_meta_matches(meta: dict, symbol: str, quote_currency: str) -> bool:
    """Verify that Yahoo chart metadata belongs to the requested crypto pair.

    Ticker-like crypto queries are ambiguous (for example BTG is also used by
    non-crypto securities). A successful chart URL alone is not enough: accept
    the direct pair only when Yahoo identifies it as cryptocurrency/CCC and the
    returned symbol/currency match the requested pair.
    """
    requested = str(symbol or "").upper().strip()
    returned = str((meta or {}).get("symbol") or requested).upper().strip()
    currency = str((meta or {}).get("currency") or "").upper().strip()
    instrument_type = str((meta or {}).get("instrumentType") or "").upper().strip()
    exchange = str((meta or {}).get("exchangeName") or (meta or {}).get("fullExchangeName") or "").upper().strip()
    return (
        bool(requested)
        and returned == requested
        and currency == str(quote_currency or "").upper().strip()
        and (instrument_type in {"CRYPTOCURRENCY", "CRYPTO"} or exchange == "CCC")
    )
