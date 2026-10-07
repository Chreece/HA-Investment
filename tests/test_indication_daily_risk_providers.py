"""Daily risk feeds retain observed session calendars and never request weekly bars."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest


COMPONENT = Path(__file__).resolve().parents[1] / "custom_components/investment"
DAY = 86400
NOW = int(datetime(2026, 10, 6, 18, tzinfo=timezone.utc).timestamp())


def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


@pytest.fixture(scope="module")
def modules():
    """Load actual adapters with only Home Assistant's session acquisition stubbed."""
    patch = pytest.MonkeyPatch()
    package_name = "_investment_daily_risk_runtime"
    package = ModuleType(package_name)
    package.__path__ = [str(COMPONENT)]
    patch.setitem(sys.modules, package_name, package)
    for name, values in (
        ("homeassistant", {}), ("homeassistant.core", {"HomeAssistant": object}),
        ("homeassistant.helpers", {}),
        ("homeassistant.helpers.aiohttp_client", {"async_get_clientsession": lambda hass: None}),
    ):
        module = ModuleType(name)
        module.__path__ = []
        module.__dict__.update(values)
        patch.setitem(sys.modules, name, module)
    loaded = SimpleNamespace(**{
        label: importlib.import_module(f"{package_name}.{relative}")
        for label, relative in (
            ("yahoo", "providers.yahoo"), ("twelve", "providers.twelve_data"),
            ("kraken", "providers.kraken"), ("base", "providers.base"),
            ("models", "models"), ("quality", "data_quality"),
        )
    })
    yield loaded
    for name in list(sys.modules):
        if name.startswith(package_name + "."):
            sys.modules.pop(name, None)
    patch.undo()


@pytest.fixture(autouse=True)
def source_clock(modules, monkeypatch):
    for provider in (modules.yahoo, modules.twelve, modules.kraken):
        monkeypatch.setattr(provider, "time", SimpleNamespace(time=lambda: NOW))


def yahoo_chart(*, timestamps=None, zone="Europe/Berlin", interval="1d"):
    timestamps = timestamps or [stamp("2026-10-04T22:00:00Z"), stamp("2026-10-05T22:00:00Z")]
    return {
        "meta": {
            "symbol": "EUNL.DE", "currency": "EUR", "exchangeName": "GER",
            "instrumentType": "ETF", "exchangeTimezoneName": zone,
            "dataGranularity": interval,
        },
        "timestamp": timestamps,
        "indicators": {
            "quote": [{"close": [200.0 + index for index in range(len(timestamps))]}],
            "adjclose": [{"adjclose": [100.0 + index for index in range(len(timestamps))]}],
        },
    }


def yahoo_provider(modules, payload):
    provider = modules.yahoo.YahooProvider(None)
    provider._get_json = AsyncMock(return_value={"chart": {"result": [deepcopy(payload)]}})
    return provider


def adjusted_risk(provider):
    return asyncio.run(provider.async_adjusted_history(
        "EUNL.DE", "5y_risk", expected_currency="EUR", require_exact_symbol=True,
        expected_exchange="Xetra", expected_category="etf",
    ))


def test_yahoo_requests_adjusted_daily_risk_and_retains_source_instants(modules):
    payload = yahoo_chart()
    provider = yahoo_provider(modules, payload)
    points = adjusted_risk(provider)
    args, params = provider._get_json.call_args
    assert args == ("https://query1.finance.yahoo.com/v8/finance/chart/EUNL.DE",)
    assert params["range"] == "5y"
    assert params["interval"] == "1d"
    assert params["includeAdjustedClose"] == "true"
    assert [point.ts for point in points] == payload["timestamp"]
    assert [point.value for point in points] == [100.0, 101.0]
    assert [point.session_date for point in points] == ["2026-10-05", "2026-10-06"]
    restored = [modules.models.HistoryPoint(**point.as_dict()) for point in points]
    assert restored == points


@pytest.mark.parametrize(("zone", "raw_stamp", "session_date"), [
    ("Europe/Berlin", "2026-03-22T23:00:00Z", "2026-03-23"),
    ("Europe/Berlin", "2026-03-29T22:00:00Z", "2026-03-30"),
    ("Pacific/Auckland", "2026-10-04T11:00:00Z", "2026-10-05"),
    ("Pacific/Honolulu", "2026-10-05T09:00:00Z", "2026-10-04"),
])
def test_yahoo_session_calendar_uses_observed_zone_and_historical_dst(modules, zone, raw_stamp, session_date):
    source_ts = stamp(raw_stamp)
    provider = yahoo_provider(modules, yahoo_chart(timestamps=[source_ts], zone=zone))
    point = adjusted_risk(provider)[0]
    assert point.ts == source_ts
    assert point.session_date == session_date


@pytest.mark.parametrize("zone", [None, "", "Mars/Olympus", "../Europe/Berlin", False])
def test_yahoo_daily_risk_abstains_without_valid_source_session_timezone(modules, zone):
    provider = yahoo_provider(modules, yahoo_chart(zone=zone))
    with pytest.raises(modules.base.ProviderError, match="session timezone"):
        adjusted_risk(provider)


@pytest.mark.parametrize("interval", [None, "", "1wk", "1h", False])
def test_yahoo_weekly_or_unidentified_response_cannot_satisfy_daily_risk(modules, interval):
    provider = yahoo_provider(modules, yahoo_chart(interval=interval))
    with pytest.raises(modules.base.ProviderError, match="interval"):
        adjusted_risk(provider)


def test_yahoo_weekly_signal_keeps_its_original_api_and_serialized_shape(modules):
    payload = yahoo_chart(interval="1wk", zone=None)
    provider = yahoo_provider(modules, payload)
    points = asyncio.run(provider.async_adjusted_history("EUNL.DE", "5y"))
    assert provider._get_json.call_args.kwargs["interval"] == "1wk"
    assert all(point.session_date is None for point in points)
    assert points[0].as_dict() == {"ts": payload["timestamp"][0], "value": 100.0}


def test_yahoo_crypto_daily_risk_uses_native_close_with_same_calendar_contract(modules):
    payload = yahoo_chart(zone="UTC")
    payload["meta"].update(symbol="BTC-EUR", instrumentType="CRYPTOCURRENCY", exchangeName="CCC")
    provider = yahoo_provider(modules, payload)
    points = asyncio.run(provider.async_history(
        "BTC-EUR", "5y_risk", require_exact_symbol=True, expected_currency="EUR",
        expected_category="crypto", expected_exchange="CCC",
    ))
    assert provider._get_json.call_args.kwargs["interval"] == "1d"
    assert "includeAdjustedClose" not in provider._get_json.call_args.kwargs
    assert [point.value for point in points] == [200.0, 201.0]
    assert [point.session_date for point in points] == ["2026-10-04", "2026-10-05"]


def twelve_payload(*, interval="1day", dates=None):
    return {
        "meta": {
            "symbol": "SAP", "currency": "EUR", "exchange": "XETRA",
            "type": "Common Stock", "interval": interval,
            "exchange_timezone": "Europe/Berlin",
        },
        "values": [{"datetime": day, "close": str(100 + index)}
                   for index, day in enumerate(dates or ["2026-10-05", "2026-10-06"])],
    }


def twelve_provider(modules, payload):
    provider = modules.twelve.TwelveDataProvider(None, "fixture-key")
    provider._get_json = AsyncMock(return_value=deepcopy(payload))
    return provider


def twelve_risk(provider):
    return asyncio.run(provider.async_history("SAP|XETRA|EUR", "5y_risk", expected_metadata={
        "symbol": "SAP", "currency": "EUR", "exchange": "Xetra", "category": "stock",
    }))


def test_twelve_requests_daily_risk_and_preserves_reported_exchange_dates(modules):
    payload = twelve_payload(dates=["2026-10-06", "2026-10-05"])
    provider = twelve_provider(modules, payload)
    points = twelve_risk(provider)
    args, params = provider._get_json.call_args
    assert args == ("time_series",)
    assert params == {
        "symbol": "SAP", "exchange": "XETRA", "interval": "1day", "outputsize": 2000,
        "order": "ASC", "timezone": "UTC",
    }
    # The UTC query does not change Twelve's exchange-local daily date labels.
    assert [point.session_date for point in points] == ["2026-10-05", "2026-10-06"]
    assert [point.ts for point in points] == [stamp("2026-10-05T00:00:00Z"), stamp("2026-10-06T00:00:00Z")]


@pytest.mark.parametrize("raw_date", [None, "", False, 1791158400, "20261005", "2026-02-30", "2026-10-05T00:00:00Z"])
def test_twelve_daily_risk_requires_actual_canonical_daily_session_date(modules, raw_date):
    payload = twelve_payload()
    payload["values"][0]["datetime"] = raw_date
    payload["values"][0]["timestamp"] = stamp("2026-10-05T00:00:00Z")
    provider = twelve_provider(modules, payload)
    with pytest.raises(modules.base.ProviderError, match="session date"):
        twelve_risk(provider)


@pytest.mark.parametrize("interval", [None, "1week", "1h"])
def test_twelve_daily_risk_rejects_weekly_or_missing_interval_metadata(modules, interval):
    provider = twelve_provider(modules, twelve_payload(interval=interval))
    with pytest.raises(modules.base.ProviderError, match="interval"):
        twelve_risk(provider)


@pytest.mark.parametrize("close", [False, 0, "nan", "inf", "-1"])
def test_twelve_daily_risk_never_launders_invalid_values_without_identity_kwargs(modules, close):
    payload = twelve_payload()
    payload["values"][0]["close"] = close
    provider = twelve_provider(modules, payload)
    with pytest.raises(modules.base.ProviderError, match="observation"):
        asyncio.run(provider.async_history("SAP|XETRA|EUR", "5y_risk"))


def test_twelve_risk_horizon_filters_old_rows_without_changing_weekly_signal(modules):
    payload = twelve_payload(dates=["2020-01-01", "2026-10-05", "2026-10-06"])
    provider = twelve_provider(modules, payload)
    assert len(twelve_risk(provider)) == 2
    payload["meta"]["interval"] = "1week"
    provider._get_json = AsyncMock(return_value=payload)
    points = asyncio.run(provider.async_history("SAP|XETRA|EUR", "5y"))
    assert provider._get_json.call_args.kwargs["interval"] == "1week"
    assert provider._get_json.call_args.kwargs["outputsize"] == 280
    assert all(point.session_date is None for point in points)


def kraken_provider(modules, timestamps):
    provider = modules.kraken.KrakenProvider(None)
    provider._get = AsyncMock(return_value={
        "XXBTZEUR": [[ts, "1", "1", "1", str(100 + index), "1", "1", 1]
                     for index, ts in enumerate(timestamps)],
        "last": timestamps[-1],
    })
    return provider


def test_kraken_daily_risk_honors_actual_720_row_bound_without_weekly_padding(modules):
    latest = stamp("2026-10-06T00:00:00Z")
    timestamps = [latest - (719 - index) * DAY for index in range(720)]
    provider = kraken_provider(modules, timestamps)
    points = asyncio.run(provider.async_history("XXBTZEUR", "5y_risk"))
    provider._get.assert_awaited_once_with(
        "OHLC", pair="XXBTZEUR", interval=1440, since=NOW - 5 * 370 * DAY,
    )
    assert len(points) == 720
    assert [point.ts for point in points] == timestamps
    assert points[-1].session_date == "2026-10-06"
    assert points[-1].ts - points[0].ts == 719 * DAY
    assert modules.quality.assess_history_freshness(
        points, analysis_time=NOW, category="crypto", cadence="daily", role="risk",
    )["eligible"] is True


@pytest.mark.parametrize("offset", [1, 3600, .001])
def test_kraken_daily_risk_requires_observed_utc_daily_boundary(modules, offset):
    provider = kraken_provider(modules, [stamp("2026-10-06T00:00:00Z") + offset])
    with pytest.raises(modules.base.ProviderError, match="session boundary"):
        asyncio.run(provider.async_history("XXBTZEUR", "5y_risk"))


def test_kraken_weekly_response_cannot_pass_daily_risk_freshness(modules):
    latest = stamp("2026-10-05T00:00:00Z")
    provider = kraken_provider(modules, [latest - 7 * DAY, latest])
    points = asyncio.run(provider.async_history("XXBTZEUR", "5y_risk"))
    result = modules.quality.assess_history_freshness(
        points, analysis_time=NOW, category="crypto", cadence="daily", role="risk",
    )
    assert result["eligible"] is False
    assert "risk_history_tail_gap" in result["reasons"]


def test_kraken_legacy_weekly_signal_remains_weekly_and_without_session_metadata(modules):
    latest = stamp("2026-10-05T00:00:00Z")
    provider = kraken_provider(modules, [latest - 7 * DAY, latest])
    points = asyncio.run(provider.async_history("XXBTZEUR", "5y"))
    assert provider._get.call_args.kwargs["interval"] == 10080
    assert [point.as_dict() for point in points] == [
        {"ts": latest - 7 * DAY, "value": 100.0}, {"ts": latest, "value": 101.0},
    ]
