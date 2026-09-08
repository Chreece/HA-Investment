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
            data = await self._get_json(
                "https://query1.finance.yahoo.com/v1/finance/search",
                q=query,
                quotesCount=40,
                newsCount=0,
                listsCount=0,
                enableFuzzyQuery="true",
            )
        except ProviderError:
            # A verified direct crypto pair is sufficient on its own. Generic
            # Yahoo discovery is useful for breadth, but a transient/rate-limited
            # search endpoint must not hide a pair we already proved exists.
            if direct_crypto is not None:
                return [direct_crypto]
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
