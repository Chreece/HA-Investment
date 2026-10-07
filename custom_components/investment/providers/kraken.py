"""Kraken public no-key crypto provider."""
from __future__ import annotations

import time
import math
from collections.abc import Sequence
from datetime import datetime, timezone

from .base import MarketProvider, ProviderError
from ..search import CRYPTO_NAMES, crypto_query_score
from ..models import HistoryPoint, Quote, SearchResult

# Long-horizon signals retain weekly sampling. Risk uses separate daily candles
# and their UTC sessions before weekly aggregation. Kraken returns at most 720
# OHLC rows regardless of `since`; 5y_risk requests a horizon, not a guarantee
# of five years of coverage, and must never substitute weekly data to fill it.
_INTERVAL = {"1d": 5, "7d": 30, "1m": 60, "3m": 240, "1y": 1440, "5y": 10080, "5y_risk": 1440}

_HORIZON = {
    "1d": 86400,
    "7d": 7 * 86400,
    "1m": 31 * 86400,
    "3m": 93 * 86400,
    "1y": 370 * 86400,
    "5y": 5 * 370 * 86400,
    "5y_risk": 5 * 370 * 86400,
}


def _clean_asset(value: str) -> str:
    value = value.upper()
    aliases = {"XBT": "BTC", "XXBT": "BTC", "XDG": "DOGE", "XXDG": "DOGE"}
    if value in aliases:
        return aliases[value]
    if len(value) == 4 and value[0] in "XZ" and value[1:].isalpha():
        value = value[1:]
    return aliases.get(value, value)


class KrakenProvider(MarketProvider):
    provider_id = "kraken"
    title = "Kraken"

    def __init__(self, hass):
        super().__init__(hass)
        self._pairs: dict[str, dict] | None = None
        self._pairs_loaded = 0.0

    async def _get(self, path: str, **params):
        try:
            async with self.session.get(
                f"https://api.kraken.com/0/public/{path}", params=params or None, timeout=12
            ) as response:
                if response.status != 200:
                    raise ProviderError(f"Kraken HTTP {response.status}")
                data = await response.json(content_type=None)
        except ProviderError:
            raise
        except Exception as err:
            raise ProviderError(f"Kraken request failed: {err}") from err
        if data.get("error"):
            raise ProviderError(", ".join(data["error"]))
        return data.get("result") or {}

    async def _asset_pairs(self) -> dict[str, dict]:
        now = time.monotonic()
        if self._pairs is None or now - self._pairs_loaded > 6 * 3600:
            self._pairs = await self._get("AssetPairs")
            self._pairs_loaded = now
        return self._pairs

    async def async_search(self, query: str, base_currency: str) -> Sequence[SearchResult]:
        q = query.strip().lower()
        if len(q) < 2:
            return []
        pairs = await self._asset_pairs()
        scored: list[tuple[int, SearchResult]] = []
        for pair_id, item in pairs.items():
            wsname = item.get("wsname") or item.get("altname") or pair_id
            if "/" in wsname:
                raw_base, raw_quote = wsname.split("/", 1)
            else:
                raw_base, raw_quote = item.get("base", ""), item.get("quote", "")
            base, quote = _clean_asset(raw_base), _clean_asset(raw_quote)
            # Portfolio search is currency-scoped. An EUR portfolio must never
            # be offered BCH/AUD, BCH/BTC, BCH/GBP, etc. Merely converting the
            # value later would make the add/search workflow ambiguous.
            if quote.upper() != base_currency.upper():
                continue
            score = crypto_query_score(q, base, pair_id, item.get("altname", ""), wsname, quote)
            if score is None:
                continue
            scored.append(
                (
                    score,
                    SearchResult(
                        provider=self.provider_id,
                        provider_id=pair_id,
                        symbol=f"{base}/{quote}",
                        name=f"{(CRYPTO_NAMES.get(base) or (base,))[0]} / {quote}",
                        category="crypto",
                        currency=quote,
                        exchange="Kraken",
                    ),
                )
            )
        scored.sort(key=lambda x: (x[0], x[1].symbol))
        return [item for _, item in scored[:12]]

    async def async_discover(
        self, base_currency: str, category: str | None = None, *, limit: int = 20
    ) -> Sequence[SearchResult]:
        """Enumerate a bounded crypto universe quoted in the portfolio currency.

        Kraken exposes its complete public AssetPairs catalog, so crypto
        discovery does not need to pretend that the user's current holdings or
        a text search are the market universe. Known liquid symbols are ordered
        first and the remaining pairs are still eligible behind them.
        """
        if category not in (None, "crypto"):
            return []
        quote_currency = str(base_currency or "").upper().strip()
        if not quote_currency:
            return []
        pairs = await self._asset_pairs()
        priority = {symbol: idx for idx, symbol in enumerate(CRYPTO_NAMES)}
        rows: list[tuple[int, str, SearchResult]] = []
        seen: set[str] = set()
        for pair_id, item in pairs.items():
            wsname = item.get("wsname") or item.get("altname") or pair_id
            if "/" in wsname:
                raw_base, raw_quote = wsname.split("/", 1)
            else:
                raw_base, raw_quote = item.get("base", ""), item.get("quote", "")
            base, quote = _clean_asset(raw_base), _clean_asset(raw_quote)
            if quote != quote_currency or not base or base == quote or base in seen:
                continue
            seen.add(base)
            known_rank = priority.get(base, len(priority) + 100)
            rows.append(
                (
                    known_rank,
                    base,
                    SearchResult(
                        provider=self.provider_id,
                        provider_id=pair_id,
                        symbol=f"{base}/{quote}",
                        name=f"{(CRYPTO_NAMES.get(base) or (base,))[0]} / {quote}",
                        category="crypto",
                        currency=quote,
                        exchange="Kraken",
                    ),
                )
            )
        rows.sort(key=lambda row: (row[0], row[1]))
        return [row[2] for row in rows[: max(1, int(limit))]]

    async def _pair_info(self, provider_id: str) -> dict:
        pairs = await self._asset_pairs()
        if provider_id not in pairs:
            raise ProviderError(f"Unknown Kraken pair {provider_id}")
        return pairs[provider_id]

    async def async_quote(self, provider_id: str) -> Quote:
        info = await self._pair_info(provider_id)
        result = await self._get("Ticker", pair=provider_id)
        ticker = result.get(provider_id)
        if not ticker:
            raise ProviderError(f"No Kraken quote for {provider_id}")
        wsname = str(info.get("wsname") or "")
        if "/" not in wsname:
            raise ProviderError(f"Kraken pair identity missing for {provider_id}")
        raw_base, raw_quote = wsname.split("/", 1)
        base_currency, quote_currency = _clean_asset(raw_base), _clean_asset(raw_quote)
        price = float(ticker["c"][0])
        market_time = None
        try:
            # Ticker has no last-trade timestamp. Bind both price and time to
            # the same actual trade, not to the time this request completed.
            trades = await self._get("Trades", pair=provider_id, count=1)
            rows = trades.get(provider_id) or []
            if rows and len(rows[-1]) >= 3:
                raw_price, raw_time = rows[-1][0], rows[-1][2]
                if not isinstance(raw_price, bool) and not isinstance(raw_time, bool):
                    price, market_time = float(raw_price), raw_time
        except ProviderError:
            # General portfolio quotes remain readable. An indication cannot
            # allocate from this undated ticker value.
            pass
        return Quote(
            price=price,
            currency=quote_currency,
            previous_close=float(ticker.get("o")) if ticker.get("o") else None,
            market_time=market_time,
            source=self.title,
            delayed=False,
            instrument_metadata={
                "provider": self.provider_id,
                "provider_id": provider_id,
                "symbol": f"{base_currency}/{quote_currency}",
                "category": "crypto" if info.get("aclass_base", "currency") == "currency" else None,
                "currency": quote_currency,
                "exchange": "Kraken",
            },
        )

    async def async_history(self, provider_id: str, period: str) -> Sequence[HistoryPoint]:
        interval = _INTERVAL.get(period, 60)
        horizon = _HORIZON.get(period, _HORIZON["1m"])
        since = int(time.time()) - horizon
        result = await self._get("OHLC", pair=provider_id, interval=interval, since=since)
        rows = result.get(provider_id) or []
        points = []
        for row in rows:
            if len(row) <= 4 or isinstance(row[0], bool) or isinstance(row[4], bool):
                raise ProviderError(f"Invalid Kraken history observation for {provider_id}")
            ts, value = row[0], float(row[4])
            if not isinstance(ts, (int, float)) or not math.isfinite(ts) or ts <= 0 or not math.isfinite(value) or value <= 0:
                raise ProviderError(f"Invalid Kraken history observation for {provider_id}")
            session_date = None
            if period == "5y_risk":
                if ts % 86400 != 0:
                    raise ProviderError(f"Kraken daily risk history session boundary invalid for {provider_id}")
                try:
                    session_date = datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
                except (OverflowError, OSError, ValueError) as err:
                    raise ProviderError(f"Invalid Kraken daily risk history timestamp for {provider_id}") from err
            points.append(HistoryPoint(ts, value, session_date=session_date))
        if not points:
            raise ProviderError(f"No Kraken history for {provider_id}")
        return points
