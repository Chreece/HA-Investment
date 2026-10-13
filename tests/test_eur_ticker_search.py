"""Yahoo search recovers verified EUR listings without guessing broker aliases."""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, call

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


def meta(symbol: str, *, currency: str = "EUR", exchange: str = "XETRA", name: str = "State Street SPDR S&P 500 UCITS ETF USD Acc"):
    return {
        "meta": {
            "symbol": symbol,
            "currency": currency,
            "instrumentType": "ETF",
            "fullExchangeName": exchange,
            "longName": name,
        }
    }


def provider(modules, *, search_rows=None, chart_rows=None, search_error=False):
    instance = modules.yahoo.YahooProvider(None)
    if search_error:
        instance._get_json = AsyncMock(side_effect=modules.base.ProviderError("search unavailable"))
    else:
        instance._get_json = AsyncMock(return_value={"quotes": list(search_rows or [])})
    chart_rows = dict(chart_rows or {})

    async def chart(symbol, range_, interval):
        payload = chart_rows.get(symbol)
        if payload is None:
            raise modules.base.ProviderError(f"No chart for {symbol}")
        return payload

    instance._chart = AsyncMock(side_effect=chart)
    return instance


def symbols(results):
    return [item.symbol for item in results]


def test_broker_eu_alias_recovers_separate_verified_eur_listings(modules):
    instance = provider(
        modules,
        chart_rows={
            "SPYL.DE": meta("SPYL.DE", exchange="XETRA"),
            "SPYL.AS": meta("SPYL.AS", exchange="Amsterdam"),
        },
    )

    results = asyncio.run(instance.async_search("SPYL.EU", "EUR"))

    assert symbols(results) == ["SPYL.DE", "SPYL.AS"]
    assert [item.currency for item in results] == ["EUR", "EUR"]
    assert [item.exchange for item in results] == ["XETRA", "Amsterdam"]
    assert [item.search_alias for item in results] == ["SPYL.EU", "SPYL.EU"]
    assert results[0].as_dict()["search_alias"] == "SPYL.EU"
    assert instance._get_json.await_args.kwargs["q"] == "SPYL.EU"


def test_plain_ticker_backfills_verified_listings_when_fuzzy_search_misses_them(modules):
    instance = provider(
        modules,
        search_rows=[
            {
                "symbol": "SPY5.DE",
                "quoteType": "ETF",
                "longname": "Different S&P 500 ETF",
                "currency": "EUR",
                "exchange": "GER",
            }
        ],
        chart_rows={
            "SPYL.DE": meta("SPYL.DE", exchange="XETRA"),
            "SPYL.AS": meta("SPYL.AS", exchange="Amsterdam"),
        },
    )

    results = asyncio.run(instance.async_search("SPYL", "EUR"))

    assert symbols(results) == ["SPYL.DE", "SPYL.AS", "SPY5.DE"]
    assert [item.search_alias for item in results] == [None, None, None]
    assert instance._chart.await_args_list[0] == call("SPYL-EUR", "1d", "1d")
    assert call("SPYL.DE", "1d", "1d") in instance._chart.await_args_list
    assert call("SPYL.AS", "1d", "1d") in instance._chart.await_args_list


def test_direct_listing_probe_requires_exact_symbol_and_eur_currency(modules):
    instance = provider(
        modules,
        chart_rows={
            "SPYL.DE": meta("OTHER.DE", exchange="XETRA"),
            "SPYL.AS": meta("SPYL.AS", currency="USD", exchange="Amsterdam"),
        },
    )

    assert asyncio.run(instance.async_search("SPYL.EU", "EUR")) == []


def test_existing_yahoo_suffix_is_not_rewritten(modules):
    instance = provider(
        modules,
        search_rows=[
            {
                "symbol": "SPYL.DE",
                "quoteType": "ETF",
                "longname": "State Street SPDR S&P 500 UCITS ETF USD Acc",
                "currency": "EUR",
                "exchange": "GER",
            }
        ],
    )

    results = asyncio.run(instance.async_search("SPYL.DE", "EUR"))

    assert symbols(results) == ["SPYL.DE"]
    assert results[0].search_alias is None
    assert instance._chart.await_count == 0


def test_verified_direct_listing_survives_generic_search_outage(modules):
    instance = provider(
        modules,
        search_error=True,
        chart_rows={"SPYL.DE": meta("SPYL.DE", exchange="XETRA")},
    )

    results = asyncio.run(instance.async_search("SPYL.EU", "EUR"))

    assert symbols(results) == ["SPYL.DE"]


def test_search_ui_shows_broker_alias_without_replacing_provider_symbol():
    panel = (COMPONENT / "www" / "investment-panel.js").read_text()
    assert "const shownSymbol=r.search_alias||r.symbol" in panel
    assert "r.search_alias&&r.search_alias!==r.symbol" in panel
    assert "PANEL_ASSET_REVISION" not in panel
