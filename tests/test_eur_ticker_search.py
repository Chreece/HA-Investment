"""Ticker search resolves broker-style aliases across Yahoo markets."""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest


COMPONENT = Path(__file__).resolve().parents[1] / "custom_components/investment"


@pytest.fixture(scope="module")
def modules():
    patch = pytest.MonkeyPatch()
    package_name = "_investment_search_runtime"
    package = ModuleType(package_name)
    package.__path__ = [str(COMPONENT)]
    patch.setitem(sys.modules, package_name, package)
    for name, values in (
        ("homeassistant", {}),
        ("homeassistant.core", {"HomeAssistant": object}),
        ("homeassistant.helpers", {}),
        ("homeassistant.helpers.aiohttp_client", {"async_get_clientsession": lambda hass: None}),
    ):
        module = ModuleType(name)
        module.__path__ = []
        module.__dict__.update(values)
        patch.setitem(sys.modules, name, module)

    loaded = SimpleNamespace(
        yahoo=importlib.import_module(f"{package_name}.providers.yahoo"),
        base=importlib.import_module(f"{package_name}.providers.base"),
    )
    yield loaded
    for name in list(sys.modules):
        if name.startswith(package_name + "."):
            sys.modules.pop(name, None)
    patch.undo()


def meta(
    symbol: str,
    *,
    currency: str,
    exchange: str,
    instrument_type: str = "ETF",
    name: str | None = None,
):
    return {
        "meta": {
            "symbol": symbol,
            "currency": currency,
            "instrumentType": instrument_type,
            "fullExchangeName": exchange,
            "longName": name or symbol,
        }
    }


def provider(
    modules,
    *,
    search_rows: dict[str, list[dict]] | None = None,
    chart_rows: dict[str, dict] | None = None,
    search_error: bool = False,
):
    instance = modules.yahoo.YahooProvider(None)
    search_rows = dict(search_rows or {})
    chart_rows = dict(chart_rows or {})

    async def get_json(url, **params):
        if search_error:
            raise modules.base.ProviderError("search unavailable")
        return {"quotes": list(search_rows.get(str(params.get("q") or ""), []))}

    async def chart(symbol, range_, interval):
        payload = chart_rows.get(symbol)
        if payload is None:
            raise modules.base.ProviderError(f"No chart for {symbol}")
        return payload

    instance._get_json = AsyncMock(side_effect=get_json)
    instance._chart = AsyncMock(side_effect=chart)
    return instance


def symbols(results):
    return [item.symbol for item in results]


def test_eu_alias_falls_back_to_verified_yahoo_markets_when_fuzzy_search_misses(modules):
    instance = provider(
        modules,
        search_rows={"SPYL.EU": [], "SPYL": []},
        chart_rows={
            "SPYL.DE": meta("SPYL.DE", currency="EUR", exchange="XETRA"),
            "SPYL.AS": meta("SPYL.AS", currency="EUR", exchange="Amsterdam"),
        },
    )

    results = asyncio.run(instance.async_search("SPYL.EU", "EUR"))

    assert symbols(results)[:2] == ["SPYL.DE", "SPYL.AS"]
    assert [item.search_alias for item in results[:2]] == ["SPYL.EU", "SPYL.EU"]
    assert [item.currency for item in results[:2]] == ["EUR", "EUR"]
    assert results[0].as_dict()["search_alias"] == "SPYL.EU"


def test_plain_ticker_uses_same_market_agnostic_fallback_without_alias(modules):
    instance = provider(
        modules,
        search_rows={
            "SPYL": [
                {
                    "symbol": "SPY5.DE",
                    "quoteType": "ETF",
                    "longname": "Different S&P 500 ETF",
                    "currency": "EUR",
                    "exchange": "GER",
                }
            ]
        },
        chart_rows={
            "SPYL.DE": meta("SPYL.DE", currency="EUR", exchange="XETRA"),
            "SPYL.AS": meta("SPYL.AS", currency="EUR", exchange="Amsterdam"),
        },
    )

    results = asyncio.run(instance.async_search("SPYL", "EUR"))

    assert symbols(results)[:2] == ["SPYL.DE", "SPYL.AS"]
    assert [item.search_alias for item in results[:2]] == [None, None]
    assert "SPY5.DE" in symbols(results)


def test_canadian_broker_alias_resolves_provider_discovered_tsx_listing(modules):
    instance = provider(
        modules,
        search_rows={
            "SHOP.CA": [],
            "SHOP": [
                {
                    "symbol": "SHOP.TO",
                    "quoteType": "EQUITY",
                    "currency": "CAD",
                    "exchange": "TOR",
                }
            ],
        },
        chart_rows={
            "SHOP.TO": meta(
                "SHOP.TO",
                currency="CAD",
                exchange="Toronto",
                instrument_type="EQUITY",
                name="Shopify Inc.",
            )
        },
    )

    results = asyncio.run(instance.async_search("SHOP.CA", "CAD"))

    assert symbols(results) == ["SHOP.TO"]
    assert results[0].search_alias == "SHOP.CA"
    assert results[0].currency == "CAD"


def test_uk_broker_alias_accepts_pence_listing_for_gbp_search(modules):
    instance = provider(
        modules,
        search_rows={
            "VOD.UK": [],
            "VOD": [
                {
                    "symbol": "VOD.L",
                    "quoteType": "EQUITY",
                    "currency": "GBp",
                    "exchange": "LSE",
                }
            ],
        },
        chart_rows={
            "VOD.L": meta(
                "VOD.L",
                currency="GBp",
                exchange="London",
                instrument_type="EQUITY",
                name="Vodafone Group Plc",
            )
        },
    )

    results = asyncio.run(instance.async_search("VOD.UK", "GBP"))

    assert symbols(results) == ["VOD.L"]
    assert results[0].search_alias == "VOD.UK"
    assert results[0].currency == "GBp"


def test_us_broker_alias_resolves_bare_yahoo_symbol(modules):
    instance = provider(
        modules,
        search_rows={
            "AAPL.US": [],
            "AAPL": [
                {
                    "symbol": "AAPL",
                    "quoteType": "EQUITY",
                    "currency": "USD",
                    "exchange": "NMS",
                }
            ],
        },
        chart_rows={
            "AAPL": meta(
                "AAPL",
                currency="USD",
                exchange="NasdaqGS",
                instrument_type="EQUITY",
                name="Apple Inc.",
            )
        },
    )

    results = asyncio.run(instance.async_search("AAPL.US", "USD"))

    assert symbols(results) == ["AAPL"]
    assert results[0].search_alias == "AAPL.US"


def test_real_yahoo_suffix_remains_canonical_not_an_alias(modules):
    instance = provider(
        modules,
        search_rows={
            "SHOP.TO": [
                {
                    "symbol": "SHOP.TO",
                    "quoteType": "EQUITY",
                    "longname": "Shopify Inc.",
                    "currency": "CAD",
                    "exchange": "TOR",
                }
            ]
        },
    )

    results = asyncio.run(instance.async_search("SHOP.TO", "CAD"))

    assert symbols(results) == ["SHOP.TO"]
    assert results[0].search_alias is None
    assert instance._chart.await_count == 0


def test_recovery_rejects_wrong_currency_and_wrong_symbol_metadata(modules):
    instance = provider(
        modules,
        search_rows={"7203.JP": [], "7203": []},
        chart_rows={
            "7203.T": meta(
                "OTHER.T",
                currency="JPY",
                exchange="Tokyo",
                instrument_type="EQUITY",
            ),
            "7203.SW": meta(
                "7203.SW",
                currency="CHF",
                exchange="SIX",
                instrument_type="EQUITY",
            ),
        },
    )

    assert asyncio.run(instance.async_search("7203.JP", "JPY")) == []


def test_market_registry_recovers_japan_listing_during_search_outage(modules):
    instance = provider(
        modules,
        search_error=True,
        chart_rows={
            "7203.T": meta(
                "7203.T",
                currency="JPY",
                exchange="Tokyo",
                instrument_type="EQUITY",
                name="Toyota Motor Corporation",
            )
        },
    )

    results = asyncio.run(instance.async_search("7203.JP", "JPY"))

    assert symbols(results) == ["7203.T"]
    assert results[0].search_alias == "7203.JP"


def test_market_registry_covers_non_eu_quote_currencies(modules):
    suffixes = modules.yahoo._YAHOO_PRIMARY_LISTING_SUFFIXES_BY_CURRENCY
    for currency in ("USD", "CAD", "GBP", "CHF", "JPY", "HKD", "AUD", "INR", "BRL", "ZAR"):
        assert currency in suffixes
        assert suffixes[currency]


def test_search_ui_shows_broker_alias_without_replacing_provider_symbol():
    panel = (COMPONENT / "www" / "investment-panel.js").read_text()
    assert "const shownSymbol=r.search_alias||r.symbol" in panel
    assert "r.search_alias&&r.search_alias!==r.symbol" in panel
