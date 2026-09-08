"""Frankfurter no-key foreign-exchange provider."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from collections.abc import Sequence

from .base import MarketProvider, ProviderError
from ..models import HistoryPoint, Quote, SearchResult

_HORIZON_DAYS = {"1d": 7, "7d": 14, "1m": 35, "3m": 100, "1y": 380, "5y": 5 * 370}


class FrankfurterProvider(MarketProvider):
    provider_id = "frankfurter"
    title = "Frankfurter"

    def __init__(self, hass):
        super().__init__(hass)
        self._currencies: dict[str, dict] | None = None

    async def _get(self, path: str, **params):
        try:
            async with self.session.get(
                f"https://api.frankfurter.dev/v2/{path}", params=params or None, timeout=12
            ) as response:
                if response.status != 200:
                    raise ProviderError(f"Frankfurter HTTP {response.status}")
                return await response.json(content_type=None)
        except ProviderError:
            raise
        except Exception as err:
            raise ProviderError(f"Frankfurter request failed: {err}") from err

    async def _load_currencies(self) -> dict[str, dict]:
        if self._currencies is None:
            data = await self._get("currencies")
            if isinstance(data, list):
                self._currencies = {str(x.get("iso_code") or x.get("code")): x for x in data}
            elif isinstance(data, dict):
                self._currencies = data
            else:
                self._currencies = {}
        return self._currencies

    async def async_search(self, query: str, base_currency: str) -> Sequence[SearchResult]:
        query = query.strip()
        if not query:
            return []
        currencies = await self._load_currencies()
        pairs: list[tuple[str, str]] = []
        if "/" in query:
            a, b = [x.strip().upper() for x in query.split("/", 1)]
            if len(a) == 3 and len(b) == 3 and b == base_currency.upper():
                pairs.append((a, b))
        else:
            q = query.lower()
            matches: list[str] = []
            for code, info in currencies.items():
                code = code.upper()
                name = str(info.get("name") or info.get("currency") or "") if isinstance(info, dict) else str(info)
                if q == code.lower() or q in name.lower():
                    matches.append(code)
            for code in matches[:6]:
                if code != base_currency.upper():
                    pairs.append((code, base_currency.upper()))
        metals = {"XAU": "Gold", "XAG": "Silver", "XPT": "Platinum", "XPD": "Palladium"}
        return [
            SearchResult(
                provider=self.provider_id,
                provider_id=f"{a}/{b}",
                symbol=f"{a}/{b}",
                name=f"{metals.get(a, a)} / {b}",
                category="commodity" if a in metals else "fx",
                currency=b,
                exchange="Institutional reference rates",
            )
            for a, b in pairs[:10]
        ]

    async def async_discover(
        self, base_currency: str, category: str | None = None, *, limit: int = 20
    ) -> Sequence[SearchResult]:
        """Enumerate supported FX pairs against the portfolio currency."""
        if category not in (None, "fx"):
            return []
        quote = str(base_currency or "").upper().strip()
        currencies = await self._load_currencies()
        if quote not in {str(code).upper() for code in currencies}:
            return []
        majors = [
            "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
            "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "RON", "BGN",
            "TRY", "CNY", "HKD", "SGD", "INR", "KRW", "BRL", "MXN", "ZAR",
        ]
        order = {code: idx for idx, code in enumerate(majors)}
        rows: list[tuple[int, str, SearchResult]] = []
        for raw_code, info in currencies.items():
            code = str(raw_code).upper()
            if code == quote:
                continue
            name = str(info.get("name") or info.get("currency") or code) if isinstance(info, dict) else str(info)
            rows.append(
                (
                    order.get(code, len(order) + 100),
                    code,
                    SearchResult(
                        provider=self.provider_id,
                        provider_id=f"{code}/{quote}",
                        symbol=f"{code}/{quote}",
                        name=f"{name} / {quote}",
                        category="fx",
                        currency=quote,
                        exchange="Institutional reference rates",
                    ),
                )
            )
        rows.sort(key=lambda row: (row[0], row[1]))
        return [row[2] for row in rows[: max(1, int(limit))]]

    async def async_rate(
        self, base: str, quote: str, *, on_date: str | None = None
    ) -> tuple[float, str | None]:
        """Return a current or historical FX rate without an API key."""
        base = base.upper()
        quote = quote.upper()
        if base == quote:
            return 1.0, on_date
        params = {"date": on_date} if on_date else {}
        data = await self._get(f"rate/{base}/{quote}", **params)
        return float(data["rate"]), str(data.get("date") or on_date or "") or None

    async def async_quote(self, provider_id: str) -> Quote:
        base, quote = provider_id.upper().split("/", 1)
        data = await self._get(f"rate/{base}/{quote}")
        rate = float(data["rate"])
        history = sorted(await self.async_history(provider_id, "7d"), key=lambda point: point.ts)
        prev = history[-2].value if len(history) > 1 else None
        return Quote(price=rate, currency=quote, previous_close=prev, source=self.title, delayed=True)

    async def async_history(self, provider_id: str, period: str) -> Sequence[HistoryPoint]:
        base, quote = provider_id.upper().split("/", 1)
        days = _HORIZON_DAYS.get(period, 35)
        start = date.today() - timedelta(days=days)
        params = {"base": base, "quotes": quote, "from": start.isoformat()}
        if period in {"1y", "5y"}:
            params["group"] = "week" if period == "1y" else "month"
        rows = await self._get("rates", **params)
        points: list[HistoryPoint] = []
        if isinstance(rows, list):
            for row in rows:
                if str(row.get("quote", "")).upper() != quote:
                    continue
                dt = date.fromisoformat(row["date"])
                ts = int(datetime(dt.year, dt.month, dt.day, tzinfo=UTC).timestamp())
                points.append(HistoryPoint(ts, float(row["rate"])))
        if not points:
            raise ProviderError(f"No FX history for {provider_id}")
        return points
