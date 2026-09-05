"""Twelve Data API-key market data provider."""
from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from .base import MarketProvider, ProviderError
from ..models import HistoryPoint, Quote, SearchResult

_BASE_URL = "https://api.twelvedata.com"

_TYPE_CATEGORY = {
    "common stock": "stock",
    "preferred stock": "stock",
    "etf": "etf",
    "exchange-traded fund": "etf",
    "mutual fund": "fund",
    "index": "index",
    "commodity": "commodity",
    "physical currency": "fx",
    "forex": "fx",
    "digital currency": "crypto",
    "cryptocurrency": "crypto",
}

_PERIOD = {
    "1d": ("5min", 320, 24 * 3600),
    "7d": ("30min", 400, 7 * 24 * 3600),
    "1m": ("1day", 45, 31 * 24 * 3600),
    "3m": ("1day", 110, 93 * 24 * 3600),
    "1y": ("1day", 380, 370 * 24 * 3600),
    "5y": ("1week", 280, 5 * 370 * 24 * 3600),
}


def _category(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    if value in _TYPE_CATEGORY:
        return _TYPE_CATEGORY[value]
    if "etf" in value:
        return "etf"
    if "fund" in value:
        return "fund"
    if "stock" in value or "equity" in value:
        return "stock"
    if "crypto" in value or "digital" in value:
        return "crypto"
    if "forex" in value or "currency" in value:
        return "fx"
    if "index" in value:
        return "index"
    if "commod" in value:
        return "commodity"
    return "other"


def _encode_provider_id(symbol: str, exchange: str | None, currency: str | None) -> str:
    """Keep the provider's exchange/currency identity with the stored asset."""
    return "|".join(
        (
            symbol.strip(),
            str(exchange or "").strip(),
            str(currency or "").strip().upper(),
        )
    )


def _decode_provider_id(provider_id: str) -> tuple[str, str | None, str | None]:
    parts = str(provider_id or "").split("|")
    symbol = parts[0].strip() if parts else ""
    exchange = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    currency = parts[2].strip().upper() if len(parts) > 2 and parts[2].strip() else None
    if not symbol:
        raise ProviderError("Twelve Data symbol is missing")
    return symbol, exchange, currency


def _timestamp(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    raw = str(value or "").strip()
    if not raw:
        return int(time.time())
    try:
        return int(float(raw))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except ValueError as err:
        raise ProviderError(f"Invalid Twelve Data timestamp: {raw}") from err


class TwelveDataProvider(MarketProvider):
    """Licensed/API-key market-data adapter for Twelve Data."""

    provider_id = "twelve_data"
    title = "Twelve Data"

    def __init__(self, hass, api_key: str) -> None:
        super().__init__(hass)
        self.api_key = str(api_key or "").strip()
        if not self.api_key:
            raise ValueError("Twelve Data API key is required")

    async def _get_json(self, path: str, **params):
        query = {**params, "apikey": self.api_key}
        try:
            async with self.session.get(
                f"{_BASE_URL}/{path.lstrip('/')}", params=query, timeout=15
            ) as response:
                if response.status != 200:
                    raise ProviderError(f"Twelve Data HTTP {response.status}")
                data = await response.json(content_type=None)
        except ProviderError:
            raise
        except Exception as err:
            raise ProviderError(f"Twelve Data request failed: {err}") from err
        if not isinstance(data, dict):
            raise ProviderError("Twelve Data returned an invalid response")
        raw_code = data.get("code")
        try:
            error_code = int(raw_code) if raw_code is not None else 0
        except (TypeError, ValueError):
            error_code = 0
        if data.get("status") == "error" or error_code >= 400:
            message = data.get("message") or data.get("status") or "API error"
            raise ProviderError(f"Twelve Data: {message}")
        return data

    async def async_search(self, query: str, base_currency: str) -> Sequence[SearchResult]:
        data = await self._get_json("symbol_search", symbol=query, outputsize=30)
        rows = data.get("data") or []
        results: list[SearchResult] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").strip()
            currency = str(row.get("currency") or "").strip().upper()
            if not symbol or not currency or currency != str(base_currency).upper():
                continue
            exchange = str(row.get("exchange") or row.get("mic_code") or "").strip() or None
            name = str(row.get("instrument_name") or row.get("name") or symbol).strip()
            results.append(
                SearchResult(
                    provider=self.provider_id,
                    provider_id=_encode_provider_id(symbol, exchange, currency),
                    symbol=symbol,
                    name=name,
                    category=_category(row.get("instrument_type") or row.get("type")),
                    currency=currency,
                    exchange=exchange,
                )
            )
        return results[:20]

    async def async_quote(self, provider_id: str) -> Quote:
        symbol, exchange, stored_currency = _decode_provider_id(provider_id)
        params: dict[str, Any] = {"symbol": symbol}
        if exchange:
            params["exchange"] = exchange
        data = await self._get_json("quote", **params)
        price = data.get("close") or data.get("price")
        if price in (None, ""):
            raise ProviderError(f"No Twelve Data quote for {symbol}")
        currency = str(data.get("currency") or stored_currency or "").upper()
        if not currency:
            raise ProviderError(f"Twelve Data quote currency missing for {symbol}")
        previous = data.get("previous_close")
        return Quote(
            price=float(price),
            currency=currency,
            previous_close=float(previous) if previous not in (None, "") else None,
            market_time=_timestamp(data.get("timestamp") or data.get("datetime")),
            source=self.title,
            delayed=None,
        )

    async def async_history(self, provider_id: str, period: str) -> Sequence[HistoryPoint]:
        symbol, exchange, _ = _decode_provider_id(provider_id)
        interval, outputsize, horizon = _PERIOD.get(period, _PERIOD["1m"])
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "order": "ASC",
            "timezone": "UTC",
        }
        if exchange:
            params["exchange"] = exchange
        data = await self._get_json("time_series", **params)
        cutoff = int(time.time()) - horizon
        points: list[HistoryPoint] = []
        for row in data.get("values") or []:
            if not isinstance(row, dict) or row.get("close") in (None, ""):
                continue
            ts = _timestamp(row.get("datetime") or row.get("timestamp"))
            if ts >= cutoff:
                points.append(HistoryPoint(ts=ts, value=float(row["close"])))
        points.sort(key=lambda item: item.ts)
        if not points:
            raise ProviderError(f"No Twelve Data history for {symbol}")
        return points
