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
    market_time: int | None = None
    source: str | None = None
    delayed: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class HistoryPoint:
    ts: int
    value: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
