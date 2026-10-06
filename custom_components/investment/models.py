"""Shared models for HA Investment."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class SearchResult:
    provider: str
    provider_id: str
    symbol: str
    name: str
    category: str
    currency: str | None = None
    exchange: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Quote:
    price: float
    currency: str
    previous_close: float | None = None
    market_time: int | float | None = None
    source: str | None = None
    delayed: bool | None = None
    # Observed provider response fields, never copied from the candidate request.
    instrument_metadata: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class HistoryPoint:
    ts: int | float
    value: float
    # Provider-derived trading-session date. ``ts`` remains the original bar
    # timestamp for provenance/freshness; calendar grouping must not infer a
    # local session from UTC when the provider labels bars in another zone.
    session_date: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.session_date is None:
            result.pop("session_date")
        return result
