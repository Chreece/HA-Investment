"""Alpha Vantage API-key market data provider."""
from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from .base import MarketProvider, ProviderError
from ..models import HistoryPoint, Quote, SearchResult

_BASE_URL = "https://www.alphavantage.co/query"

_TYPE_CATEGORY = {
    "equity": "stock",
    "stock": "stock",
    "etf": "etf",
    "mutual fund": "fund",
    "fund": "fund",
    "index": "index",
}

_PERIOD = {
    "1d": ("TIME_SERIES_INTRADAY", "5min", 24 * 3600),
    "7d": ("TIME_SERIES_INTRADAY", "30min", 7 * 24 * 3600),
    "1m": ("TIME_SERIES_DAILY", None, 31 * 24 * 3600),
    "3m": ("TIME_SERIES_DAILY", None, 93 * 24 * 3600),
    "1y": ("TIME_SERIES_DAILY", None, 370 * 24 * 3600),
    "5y": ("TIME_SERIES_WEEKLY", None, 5 * 370 * 24 * 3600),
}


def _encode_provider_id(symbol: str, currency: str | None) -> str:
    return f"{symbol.strip()}|{str(currency or '').strip().upper()}"


def _decode_provider_id(provider_id: str) -> tuple[str, str | None]:
    raw = str(provider_id or "")
    symbol, sep, currency = raw.rpartition("|")
    if not sep:
        symbol, currency = raw, ""
    symbol = symbol.strip()
    if not symbol:
        raise ProviderError("Alpha Vantage symbol is missing")
    return symbol, currency.strip().upper() or None


def _ts(raw: str) -> int:
    try:
        parsed = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except ValueError as err:
        raise ProviderError(f"Invalid Alpha Vantage timestamp: {raw}") from err


def _series_key(data: dict[str, Any]) -> str | None:
    return next(
        (
            key
            for key in data
            if key.lower().startswith("time series")
            or key.lower().startswith("weekly time series")
        ),
        None,
    )


class AlphaVantageProvider(MarketProvider):
    """Licensed/API-key market-data adapter for Alpha Vantage."""

    provider_id = "alpha_vantage"
    title = "Alpha Vantage"

    def __init__(self, hass, api_key: str, entitlement: str = "default") -> None:
        super().__init__(hass)
        self.api_key = str(api_key or "").strip()
        if not self.api_key:
            raise ValueError("Alpha Vantage API key is required")
        self.entitlement = (
            entitlement
            if entitlement in {"default", "realtime", "delayed"}
            else "default"
        )

    async def _get_json(self, **params):
        query = {**params, "apikey": self.api_key}
        try:
            async with self.session.get(_BASE_URL, params=query, timeout=20) as response:
                if response.status != 200:
                    raise ProviderError(f"Alpha Vantage HTTP {response.status}")
                data = await response.json(content_type=None)
        except ProviderError:
            raise
        except Exception as err:
            raise ProviderError(f"Alpha Vantage request failed: {err}") from err
        if not isinstance(data, dict):
            raise ProviderError("Alpha Vantage returned an invalid response")
        message = data.get("Error Message") or data.get("Information") or data.get("Note")
        if message:
            raise ProviderError(f"Alpha Vantage: {message}")
        return data

    async def async_search(self, query: str, base_currency: str) -> Sequence[SearchResult]:
        data = await self._get_json(function="SYMBOL_SEARCH", keywords=query)
        results: list[SearchResult] = []
        for row in data.get("bestMatches") or []:
            symbol = str(row.get("1. symbol") or "").strip()
            name = str(row.get("2. name") or symbol).strip()
            category = _TYPE_CATEGORY.get(str(row.get("3. type") or "").strip().lower(), "other")
            region = str(row.get("4. region") or "").strip() or None
            currency = str(row.get("8. currency") or "").strip().upper()
            if not symbol or not currency or currency != str(base_currency).upper():
                continue
            results.append(
                SearchResult(
                    provider=self.provider_id,
                    provider_id=_encode_provider_id(symbol, currency),
                    symbol=symbol,
                    name=name,
                    category=category,
                    currency=currency,
                    exchange=region,
                )
            )
        return results[:20]

    async def async_quote(self, provider_id: str) -> Quote:
        symbol, currency = _decode_provider_id(provider_id)
        params: dict[str, Any] = {"function": "GLOBAL_QUOTE", "symbol": symbol}
        if self.entitlement != "default":
            params["entitlement"] = self.entitlement
        data = await self._get_json(**params)
        row = data.get("Global Quote") or {}
        price = row.get("05. price")
        if price in (None, ""):
            raise ProviderError(f"No Alpha Vantage quote for {symbol}")
        if not currency:
            raise ProviderError(f"Alpha Vantage quote currency missing for {symbol}")
        previous = row.get("08. previous close")
        return Quote(
            price=float(price),
            currency=currency,
            previous_close=float(previous) if previous not in (None, "") else None,
            market_time=int(time.time()),
            source=self.title,
            delayed=(
                True
                if self.entitlement == "delayed"
                else (False if self.entitlement == "realtime" else None)
            ),
        )

    async def async_history(self, provider_id: str, period: str) -> Sequence[HistoryPoint]:
        symbol, _ = _decode_provider_id(provider_id)
        function, interval, horizon = _PERIOD.get(period, _PERIOD["1m"])
        params: dict[str, Any] = {"function": function, "symbol": symbol}
        if interval:
            params["interval"] = interval
            params["outputsize"] = "full"
        elif function == "TIME_SERIES_DAILY":
            params["outputsize"] = "full" if period == "1y" else "compact"
        data = await self._get_json(**params)
        key = _series_key(data)
        if not key or not isinstance(data.get(key), dict):
            raise ProviderError(f"No Alpha Vantage history for {symbol}")
        cutoff = int(time.time()) - horizon
        points: list[HistoryPoint] = []
        for stamp, row in data[key].items():
            if not isinstance(row, dict):
                continue
            close = row.get("4. close") or row.get("5. adjusted close")
            if close in (None, ""):
                continue
            ts = _ts(stamp)
            if ts >= cutoff:
                points.append(HistoryPoint(ts=ts, value=float(close)))
        points.sort(key=lambda item: item.ts)
        if not points:
            raise ProviderError(f"No Alpha Vantage history for {symbol}")
        return points
