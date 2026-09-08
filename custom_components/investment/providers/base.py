"""Market data provider base classes."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..models import HistoryPoint, Quote, SearchResult


def normalize_quote_currency(currency: str | None) -> str | None:
    """Normalize quote-currency labels for search matching only.

    Yahoo can report London-traded instruments as GBp/GBX (pence). They still
    belong to a GBP portfolio even though valuation code must preserve the
    pence scaling separately. No other currency is silently treated as equal.
    """
    if currency is None:
        return None
    raw = str(currency).strip()
    if not raw:
        return None
    if raw in {"GBp", "GBX"}:
        return "GBP"
    return raw.upper()


def quote_currency_matches(currency: str | None, portfolio_currency: str) -> bool:
    """Return True only when a result is quoted in the portfolio currency."""
    return normalize_quote_currency(currency) == normalize_quote_currency(portfolio_currency)


class ProviderError(RuntimeError):
    """Raised when a provider cannot satisfy a request."""


class MarketProvider(ABC):
    """Base provider interface."""

    provider_id: str
    title: str

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.session = async_get_clientsession(hass)

    @abstractmethod
    async def async_search(self, query: str, base_currency: str) -> Sequence[SearchResult]:
        """Search assets."""


    @abstractmethod
    async def async_quote(self, provider_id: str) -> Quote:
        """Return latest quote."""

    @abstractmethod
    async def async_history(self, provider_id: str, period: str) -> Sequence[HistoryPoint]:
        """Return historical points."""
