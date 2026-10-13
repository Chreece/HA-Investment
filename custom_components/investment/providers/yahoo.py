"""Yahoo Finance no-key market data provider.

Yahoo's endpoints are unofficial. They are deliberately isolated behind this provider
so another free source can replace them without changing the portfolio/UI layers.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from urllib.parse import quote

from .base import MarketProvider, ProviderError, quote_currency_matches
from ..models import HistoryPoint, Quote, SearchResult
from ..search import crypto_name, crypto_symbol_from_query, target_crypto_symbol, yahoo_crypto_meta_matches

_USER_AGENT = "Mozilla/5.0 (Home Assistant; HA-Investment/0.4.0)"

_QUOTE_TYPE_CATEGORY = {
    "EQUITY": "stock",
    "ETF": "etf",
    "MUTUALFUND": "fund",
    "INDEX": "index",
    "FUTURE": "commodity",
    "CRYPTOCURRENCY": "crypto",
    "CURRENCY": "fx",
}



# Primary Yahoo equity/fund listing suffixes keyed by quote currency.
# The suffixes come from Yahoo's published market-coverage table. Search first
# asks Yahoo for the ticker root; this registry is only a bounded recovery path
# when fuzzy search does not surface a matching listing. Every candidate is
# still chart-verified for exact symbol, currency and instrument type.
_YAHOO_PRIMARY_LISTING_SUFFIXES_BY_CURRENCY = {
    "AED": (".AE",),
    "ARS": (".BA",),
    "AUD": (".AX", ".XA"),
    "BRL": (".SA",),
    "CAD": (".TO", ".V", ".CN", ".NE"),
    "CHF": (".SW",),
    "CLP": (".SN",),
    "CNY": (".SS", ".SZ"),
    "COP": (".CL",),
    "CZK": (".PR",),
    "DKK": (".CO",),
    "EGP": (".CA",),
    "EUR": (
        ".DE", ".AS", ".PA", ".MI", ".MC", ".BR", ".VI", ".HE",
        ".AT", ".IR", ".LS", ".F", ".TI", ".TL", ".RG", ".VS",
    ),
    "GBP": (".L", ".IL", ".AQ", ".XC"),
    "HKD": (".HK",),
    "HUF": (".BD",),
    "IDR": (".JK",),
    "ILS": (".TA",),
    "INR": (".NS", ".BO"),
    "ISK": (".IC",),
    "JPY": (".T",),
    "KRW": (".KS", ".KQ"),
    "KWD": (".KW",),
    "MXN": (".MX",),
    "MYR": (".KL",),
    "NOK": (".OL",),
    "NZD": (".NZ",),
    "PHP": (".PS",),
    "PLN": (".WA",),
    "QAR": (".QA",),
    "RON": (".RO",),
    "SAR": (".SAU",),
    "SEK": (".ST",),
    "SGD": (".SI",),
    "THB": (".BK",),
    "TRY": (".IS",),
    "TWD": (".TW", ".TWO"),
    "USD": ("",),
    "VND": (".VN",),
    "ZAR": (".JO",),
}

_SUPPORTED_DIRECT_LISTING_CATEGORIES = {"stock", "etf", "fund"}
_DIRECT_LISTING_RECOVERY_TIMEOUT_SECONDS = 6.0


def _ticker_query_parts(query: str) -> tuple[str, str] | None:
    """Return normalized token/root for a ticker-like search query."""
    token = str(query or "").strip().upper()
    if not token or token.count(".") > 1:
        return None
    if "." in token:
        root, suffix = token.rsplit(".", 1)
        if not suffix or len(suffix) > 5 or not suffix.isalnum():
            return None
    else:
        root = token
    if not 1 <= len(root) <= 15:
        return None
    compact = root.replace("-", "")
    if not compact or not compact.isalnum():
        return None
    return token, root


def _symbol_matches_ticker_root(symbol: str, root: str) -> bool:
    upper = str(symbol or "").strip().upper()
    wanted = str(root or "").strip().upper()
    return bool(wanted) and (upper == wanted or upper.startswith(f"{wanted}."))


_PERIOD = {
    "1d": ("1d", "5m", 24 * 3600),
    "7d": ("1mo", "30m", 7 * 24 * 3600),
    "1m": ("1mo", "1d", 31 * 24 * 3600),
    "3m": ("3mo", "1d", 93 * 24 * 3600),
    "1y": ("1y", "1d", 370 * 24 * 3600),
    "5y": ("5y", "1wk", 5 * 370 * 24 * 3600),
}


class YahooProvider(MarketProvider):
    provider_id = "yahoo"
    title = "Yahoo Finance"

    def __init__(self, hass):
        super().__init__(hass)
        self._currency_cache: dict[str, str | None] = {}
        self._fund_holdings_cache: dict[str, tuple[float, list[dict[str, float | str]]]] = {}

    async def _get_json(self, url: str, **params):
        try:
            async with self.session.get(
                url,
                params=params or None,
                headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
                timeout=12,
            ) as response:
                if response.status != 200:
                    raise ProviderError(f"Yahoo HTTP {response.status}")
                return await response.json(content_type=None)
        except ProviderError:
            raise
        except Exception as err:
            raise ProviderError(f"Yahoo request failed: {err}") from err

    @staticmethod
    def _currency_from_symbol(symbol: str, qtype: str) -> str | None:
        """Infer quote currency where Yahoo's search payload makes it explicit."""
        upper = symbol.upper()
        if qtype == "CRYPTOCURRENCY" and "-" in upper:
            quote = upper.rsplit("-", 1)[-1]
            return quote if len(quote) == 3 else None
        if qtype == "CURRENCY" and upper.endswith("=X"):
            compact = upper[:-2].replace("=", "")
            return compact[-3:] if len(compact) == 6 else None
        return None

    async def _search_result_currency(self, symbol: str, qtype: str, reported: str | None) -> str | None:
        """Resolve the trading currency without guessing from an exchange name."""
        if reported:
            return str(reported)
        inferred = self._currency_from_symbol(symbol, qtype)
        if inferred:
            return inferred
        if symbol in self._currency_cache:
            return self._currency_cache[symbol]
        try:
            # Yahoo's search response frequently omits currency. The chart meta
            # is authoritative enough for filtering and avoids mixing USD/GBP/etc
            # listings into a portfolio explicitly configured for EUR. Cache the
            # resolved currency because adjacent keystroke searches repeat symbols.
            result = await self._chart(symbol, "1d", "1d")
            currency = (result.get("meta") or {}).get("currency")
            resolved = str(currency) if currency else None
            self._currency_cache[symbol] = resolved
            return resolved
        except ProviderError:
            return None

    async def _search_quotes(self, query: str) -> list[dict]:
        data = await self._get_json(
            "https://query1.finance.yahoo.com/v1/finance/search",
            q=query,
            quotesCount=40,
            newsCount=0,
            listsCount=0,
            enableFuzzyQuery="true",
        )
        return list(data.get("quotes", []))

    async def _verified_listing(
        self,
        symbol: str,
        base_currency: str,
        *,
        search_alias: str | None = None,
    ) -> SearchResult | None:
        """Return a listing only after Yahoo chart metadata proves its identity."""
        try:
            result = await self._chart(symbol, "1d", "1d")
        except ProviderError:
            return None
        meta = result.get("meta") or {}
        resolved = str(meta.get("symbol") or "").strip().upper()
        currency = str(meta.get("currency") or "").strip()
        qtype = str(meta.get("instrumentType") or "").upper().strip()
        category = _QUOTE_TYPE_CATEGORY.get(qtype, "other")
        if resolved != str(symbol or "").strip().upper():
            return None
        if not quote_currency_matches(currency, base_currency):
            return None
        if category not in _SUPPORTED_DIRECT_LISTING_CATEGORIES:
            return None
        name = meta.get("longName") or meta.get("shortName") or symbol
        exchange = meta.get("fullExchangeName") or meta.get("exchangeName")
        self._currency_cache[symbol] = currency
        return SearchResult(
            provider=self.provider_id,
            provider_id=symbol,
            symbol=symbol,
            name=str(name),
            category=category,
            currency=currency,
            exchange=exchange,
            search_alias=search_alias,
        )

    async def _recover_ticker_listings(
        self,
        query: str,
        base_currency: str,
        initial_quotes: Sequence[dict] = (),
    ) -> list[SearchResult]:
        """Recover a quoteable listing without assuming the user's market suffix."""
        parts = _ticker_query_parts(query)
        if parts is None:
            return []
        token, root = parts

        # A real Yahoo ticker always wins. This keeps explicit symbols such as
        # VOD.L or SHOP.TO canonical instead of treating their suffix as an alias.
        direct = await self._verified_listing(token, base_currency)
        if direct is not None:
            return [direct]

        alias = token if token != root else None
        candidate_symbols: list[str] = []

        def collect(rows: Sequence[dict]) -> None:
            for item in rows:
                symbol = str(item.get("symbol") or "").strip().upper()
                qtype = str(item.get("quoteType") or "").upper().strip()
                category = _QUOTE_TYPE_CATEGORY.get(qtype, "other")
                if (
                    symbol
                    and category in _SUPPORTED_DIRECT_LISTING_CATEGORIES
                    and _symbol_matches_ticker_root(symbol, root)
                    and symbol not in candidate_symbols
                ):
                    candidate_symbols.append(symbol)

        collect(initial_quotes)
        if token != root:
            try:
                collect(await self._search_quotes(root))
            except ProviderError:
                pass

        # Verify provider-discovered candidates before any suffix probing.
        verified: list[SearchResult] = []
        for symbol in candidate_symbols:
            item = await self._verified_listing(symbol, base_currency, search_alias=alias)
            if item is not None:
                verified.append(item)
        if verified:
            return verified

        # Yahoo fuzzy search can omit valid listings (the SPYL case is one
        # example). Probe only the documented primary suffixes for the requested
        # quote currency, with a strict time bound and exact metadata checks.
        suffixes = _YAHOO_PRIMARY_LISTING_SUFFIXES_BY_CURRENCY.get(
            str(base_currency or "").upper().strip(), ()
        )
        symbols = []
        for suffix in suffixes:
            symbol = f"{root}{suffix}"
            if symbol not in symbols and symbol != token:
                symbols.append(symbol)
        if not symbols:
            return []

        sem = asyncio.Semaphore(6)

        async def verify(symbol: str) -> SearchResult | None:
            async with sem:
                return await self._verified_listing(
                    symbol, base_currency, search_alias=alias
                )

        tasks = [asyncio.create_task(verify(symbol)) for symbol in symbols]
        done, pending = await asyncio.wait(
            tasks, timeout=_DIRECT_LISTING_RECOVERY_TIMEOUT_SECONDS
        )
        for task in pending:
            task.cancel()
        recovered: list[SearchResult] = []
        for task in done:
            if task.cancelled():
                continue
            try:
                item = task.result()
            except Exception:
                continue
            if item is not None:
                recovered.append(item)
        order = {symbol: index for index, symbol in enumerate(symbols)}
        recovered.sort(key=lambda item: order.get(item.provider_id, len(order)))
        return recovered

    async def async_search(self, query: str, base_currency: str) -> Sequence[SearchResult]:
        direct_crypto: SearchResult | None = None
        crypto_base = crypto_symbol_from_query(query)
        if crypto_base:
            target_symbol = f"{crypto_base}-{base_currency.upper()}"
            try:
                target = await self._chart(target_symbol, "1d", "1d")
                meta = target.get("meta") or {}
                target_currency = meta.get("currency")
                # A hyphenated URL is not proof of crypto. Yahoo also has many
                # ambiguous ticker/name matches, so require authoritative chart
                # metadata before exposing the direct currency-pure pair.
                if yahoo_crypto_meta_matches(meta, target_symbol, base_currency):
                    self._currency_cache[target_symbol] = str(target_currency)
                    direct_crypto = SearchResult(
                        provider=self.provider_id,
                        provider_id=target_symbol,
                        symbol=f"{crypto_base}/{base_currency.upper()}",
                        name=f"{crypto_name(crypto_base)} / {base_currency.upper()}",
                        category="crypto",
                        currency=str(target_currency),
                        exchange="CCC",
                    )
            except ProviderError:
                pass

        try:
            quotes = await self._search_quotes(query)
            data = {"quotes": quotes}
        except ProviderError:
            # Verified direct candidates are sufficient on their own. Generic
            # Yahoo discovery is useful for breadth, but a transient/rate-limited
            # search endpoint must not hide an independently verified listing.
            if direct_crypto is not None:
                return [direct_crypto]
            recovered = await self._recover_ticker_listings(query, base_currency)
            if recovered:
                return recovered
            raise
        candidates: list[tuple[dict, str, str, str, str]] = []
        for item in data.get("quotes", []):
            symbol = item.get("symbol")
            if not symbol:
                continue
            qtype = str(item.get("quoteType") or "").upper()
            category = _QUOTE_TYPE_CATEGORY.get(qtype, "other")
            name = item.get("longname") or item.get("shortname") or item.get("name") or symbol
            candidates.append((item, str(symbol), qtype, category, str(name)))

        # Resolve unknown currencies concurrently, but keep the fan-out small so
        # free Yahoo endpoints are not hammered by a single keystroke/search.
        sem = asyncio.Semaphore(4)

        async def build(candidate):
            item, symbol, qtype, category, name = candidate
            async with sem:
                currency = await self._search_result_currency(symbol, qtype, item.get("currency"))

                # Yahoo name searches frequently return only BTC-USD/BCH-USD in
                # the first result set. Do not discard the asset just because
                # that discovery row is USD: resolve the same crypto base in the
                # user's selected portfolio currency and verify it with chart
                # metadata before returning it. This keeps Search currency-pure
                # without making searches such as "Bitcoin Cash" return empty.
                if qtype == "CRYPTOCURRENCY" and not quote_currency_matches(currency, base_currency):
                    upper = symbol.upper()
                    target_symbol = target_crypto_symbol(upper, base_currency)
                    if target_symbol:
                        crypto_base = target_symbol.rsplit("-", 1)[0]
                        try:
                            target = await self._chart(target_symbol, "1d", "1d")
                            target_meta = target.get("meta") or {}
                            target_currency = target_meta.get("currency")
                            if yahoo_crypto_meta_matches(target_meta, target_symbol, base_currency):
                                self._currency_cache[target_symbol] = str(target_currency)
                                return SearchResult(
                                    provider=self.provider_id,
                                    provider_id=target_symbol,
                                    symbol=f"{crypto_base}/{base_currency.upper()}",
                                    name=f"{crypto_name(crypto_base)} / {base_currency.upper()}",
                                    category=category,
                                    currency=str(target_currency),
                                    exchange=item.get("exchDisp") or item.get("exchange"),
                                )
                        except ProviderError:
                            pass

            if not quote_currency_matches(currency, base_currency):
                return None
            return SearchResult(
                provider=self.provider_id,
                provider_id=symbol,
                symbol=symbol,
                name=name,
                category=category,
                currency=currency,
                exchange=item.get("exchDisp") or item.get("exchange"),
            )

        built = await asyncio.gather(*(build(candidate) for candidate in candidates))
        results = [item for item in built if item is not None]
        if direct_crypto is not None:
            results = [
                direct_crypto,
                *[
                    item for item in results
                    if not (
                        item.category == "crypto"
                        and item.symbol.upper() == direct_crypto.symbol.upper()
                    )
                ],
            ]
        else:
            parts = _ticker_query_parts(query)
            if parts is not None:
                token, root = parts
                exact_query_found = any(
                    str(item.provider_id or "").upper() == token for item in results
                )
                related_root_found = any(
                    _symbol_matches_ticker_root(item.provider_id, root)
                    for item in results
                )
                if (token != root and not exact_query_found) or (
                    token == root and not related_root_found
                ):
                    recovered = await self._recover_ticker_listings(
                        query, base_currency, data.get("quotes", [])
                    )
                    if recovered:
                        recovered_ids = {
                            item.provider_id.upper() for item in recovered
                        }
                        results = [
                            *recovered,
                            *[
                                item for item in results
                                if str(item.provider_id or "").upper()
                                not in recovered_ids
                            ],
                        ]
        return results


    async def _chart(self, symbol: str, range_: str, interval: str):
        safe_symbol = quote(symbol, safe="")
        data = await self._get_json(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{safe_symbol}",
            range=range_,
            interval=interval,
            includePrePost="false",
            events="div,splits",
        )
        chart = data.get("chart", {})
        if chart.get("error"):
            raise ProviderError(str(chart["error"]))
        result = (chart.get("result") or [None])[0]
        if not result:
            raise ProviderError(f"No Yahoo chart data for {symbol}")
        return result

    async def async_quote(self, provider_id: str) -> Quote:
        result = await self._chart(provider_id, "5d", "1d")
        meta = result.get("meta", {})
        closes = (((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
        usable = [float(v) for v in closes if v is not None]
        price = meta.get("regularMarketPrice")
        if price is None and usable:
            price = usable[-1]
        if price is None:
            raise ProviderError(f"No latest price for {provider_id}")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if prev is None and len(usable) > 1:
            prev = usable[-2]
        return Quote(
            price=float(price),
            currency=str(meta.get("currency") or "USD"),
            previous_close=float(prev) if prev is not None else None,
            market_time=int(meta.get("regularMarketTime") or time.time()),
            source=self.title,
            delayed=None,
        )

    async def async_history(self, provider_id: str, period: str) -> Sequence[HistoryPoint]:
        range_, interval, horizon = _PERIOD.get(period, _PERIOD["1m"])
        result = await self._chart(provider_id, range_, interval)
        timestamps = result.get("timestamp") or []
        closes = (((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
        cutoff = int(time.time()) - horizon
        points = [
            HistoryPoint(int(ts), float(value))
            for ts, value in zip(timestamps, closes, strict=False)
            if value is not None and int(ts) >= cutoff
        ]
        if not points:
            raise ProviderError(f"No history for {provider_id}")
        return points
