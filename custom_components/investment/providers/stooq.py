"""Stooq no-key fallback for US equity/ETF daily history."""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from collections.abc import Sequence

from .base import MarketProvider, ProviderError
from ..models import HistoryPoint, Quote, SearchResult

_DAYS = {"1d": 7, "7d": 14, "1m": 35, "3m": 100, "1y": 380, "5y": 5 * 370}


class StooqProvider(MarketProvider):
    provider_id = "stooq"
    title = "Stooq"

    async def async_search(self, query: str, base_currency: str) -> Sequence[SearchResult]:
        # Stooq does not expose a stable no-key global search API. Yahoo provides
        # discovery; Stooq is intentionally used only as an equity history fallback.
        return []

    @staticmethod
    def yahoo_to_stooq(symbol: str) -> str | None:
        symbol = symbol.upper()
        # Conservative mapping: plain US tickers only. Avoid guessing exchanges.
        if symbol.replace("-", "").isalnum() and "." not in symbol and "=" not in symbol and "/" not in symbol:
            return f"{symbol.lower()}.us"
        return None

    async def _history_for_symbol(self, symbol: str, period: str) -> list[HistoryPoint]:
        stooq = self.yahoo_to_stooq(symbol)
        if not stooq:
            raise ProviderError("No safe Stooq mapping")
        days = _DAYS.get(period, 35)
        end = date.today()
        start = end - timedelta(days=days)
        try:
            async with self.session.get(
                "https://stooq.com/q/d/l/",
                params={"s": stooq, "i": "d", "d1": start.strftime("%Y%m%d"), "d2": end.strftime("%Y%m%d")},
                timeout=12,
            ) as response:
                if response.status != 200:
                    raise ProviderError(f"Stooq HTTP {response.status}")
                text = await response.text()
        except ProviderError:
            raise
        except Exception as err:
            raise ProviderError(f"Stooq request failed: {err}") from err
        reader = csv.DictReader(io.StringIO(text))
        points: list[HistoryPoint] = []
        from datetime import datetime
        for row in reader:
            if not row.get("Date") or not row.get("Close") or row["Close"] == "N/D":
                continue
            dt = datetime.strptime(row["Date"], "%Y-%m-%d")
            points.append(HistoryPoint(int(dt.timestamp()), float(row["Close"])))
        if not points:
            raise ProviderError(f"No Stooq history for {symbol}")
        return points

    async def async_quote(self, provider_id: str) -> Quote:
        points = await self._history_for_symbol(provider_id, "7d")
        return Quote(
            price=points[-1].value,
            currency="USD",
            previous_close=points[-2].value if len(points) > 1 else None,
            market_time=points[-1].ts,
            source=self.title,
            delayed=True,
        )

    async def async_history(self, provider_id: str, period: str) -> Sequence[HistoryPoint]:
        return await self._history_for_symbol(provider_id, period)
