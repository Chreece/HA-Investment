"""Exercise the production indication boundary with deterministic provider payloads.

Only Home Assistant I/O and external responses are replaced. Provider parsing,
manager caches, the runtime wrapper, scoring and allocation run unchanged. This
catches information lost between individually valid helper functions.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import importlib
import json
import math
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest


COMP = Path(__file__).resolve().parents[1] / "custom_components/investment"
NOW = int(datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc).timestamp())
DAY = 86400


@pytest.fixture(scope="module")
def modules():
    """Import the shipped package without booting Home Assistant or touching I/O."""
    patch = pytest.MonkeyPatch()
    package_name = "_investment_evidence_runtime"
    package = ModuleType(package_name)
    package.__path__ = [str(COMP)]
    patch.setitem(sys.modules, package_name, package)

    def stub(name, **values):
        mod = ModuleType(name)
        mod.__dict__.update(values)
        mod.__path__ = []
        patch.setitem(sys.modules, name, mod)
        return mod

    stub("homeassistant")
    stub("homeassistant.core", HomeAssistant=object, Context=object)
    stub("homeassistant.config_entries", ConfigEntry=object)
    stub("homeassistant.helpers")
    stub("homeassistant.helpers.dispatcher", async_dispatcher_send=lambda *a, **k: None)
    stub("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: None)
    stub("homeassistant.helpers.storage", Store=object)
    util = stub("homeassistant.util")
    util.dt = stub("homeassistant.util.dt", now=lambda: datetime.now(timezone.utc))

    result = SimpleNamespace()
    for label, relative in (
        ("core", "manager"), ("runtime", "runtime_manager"),
        ("models", "models"), ("yahoo", "providers.yahoo"),
        ("alpha", "providers.alpha_vantage"), ("twelve", "providers.twelve_data"),
        ("kraken", "providers.kraken"), ("base", "providers.base"),
    ):
        setattr(result, label, importlib.import_module(f"{package_name}.{relative}"))
    yield result
    for name in list(sys.modules):
        if name.startswith(package_name + "."):
            sys.modules.pop(name, None)
    patch.undo()


def _daily_timestamps(count=260, end=NOW - 3600):
    out = []
    stamp = end
    while len(out) < count:
        if datetime.fromtimestamp(stamp, timezone.utc).weekday() < 5:
            out.append(stamp)
        stamp -= DAY
    return list(reversed(out))


def _weekly_timestamps(count=220):
    # Monday observations, with the latest on the day before the analysis.
    end = NOW - DAY
    return [end - 7 * DAY * (count - 1 - index) for index in range(count)]


def _prices(stamps):
    return [
        100.0 * math.exp(.00065 * (stamp - NOW) / DAY)
        * (1.0 + .004 * math.sin((stamp - NOW) / (DAY * 9)))
        for stamp in stamps
    ]


def _quote_payload(*, stamp=NOW - 3600, symbol="SAP.DE", currency="EUR", exchange="GER"):
    return {
        "meta": {
            "symbol": symbol, "currency": currency, "exchangeName": exchange,
            "fullExchangeName": "XETRA", "instrumentType": "EQUITY",
            "exchangeTimezoneName": "Europe/Berlin", "dataGranularity": "1d",
            "regularMarketPrice": _prices([NOW - 3600])[0],
            "regularMarketTime": stamp,
        },
        "timestamp": [NOW - DAY, NOW - 3600],
        "indicators": {"quote": [{"close": _prices([NOW - DAY, NOW - 3600])}]},
    }


@pytest.fixture
def environment(modules, monkeypatch):
    clock = SimpleNamespace(now=NOW)
    fake_time = SimpleNamespace(time=lambda: clock.now, monotonic=time.monotonic)
    for module in (modules.core, modules.yahoo, modules.alpha, modules.twelve, modules.kraken):
        monkeypatch.setattr(module, "time", fake_time)

    quote_payload = _quote_payload()
    history_payloads = {}
    for period, stamps, interval in (
        ("1y", _daily_timestamps(), "1d"),
        ("5y", _weekly_timestamps(), "1wk"),
        ("5y_risk", _daily_timestamps(count=1000), "1d"),
    ):
        prices = _prices(stamps)
        history_payloads[period] = {
            "meta": {**deepcopy(quote_payload["meta"]), "dataGranularity": interval},
            "timestamp": stamps,
            "indicators": {"quote": [{"close": prices}], "adjclose": [{"adjclose": prices}]},
        }

    yahoo = modules.yahoo.YahooProvider(None)

    async def chart(provider_id, range_, interval, **kwargs):
        period = "5y_risk" if (range_, interval) == ("5y", "1d") else range_
        return deepcopy(quote_payload if range_ == "5d" else history_payloads[period])

    yahoo._chart = AsyncMock(side_effect=chart)
    manager = object.__new__(modules.runtime.InvestmentManager)
    manager.yahoo = yahoo
    manager.stooq = SimpleNamespace(async_quote=AsyncMock(side_effect=modules.base.ProviderError("offline")))
    manager.frankfurter = SimpleNamespace(async_history=AsyncMock(), async_rate=AsyncMock())
    manager.providers = {"yahoo": yahoo}
    manager._cache = modules.core.TTLCache()
    manager._network_sem = asyncio.Semaphore(4)
    manager._fx_history_locks = {}
    manager.store = SimpleNamespace(
        async_user=AsyncMock(return_value={
            "base_currency": "EUR", "language": "en", "holdings": [],
            "developer_indicator_unlocked": True,
            "indication_disclaimer_version": modules.core.INDICATION_DISCLAIMER_VERSION,
            "indication_disclaimer_region": "germany",
        }),
        async_set_preferences=AsyncMock(),
    )
    monkeypatch.setattr(modules.core.InvestmentManager, "async_portfolio",
                        AsyncMock(return_value={"holdings": [], "categories": [], "total": 0}))
    manager._candidate_portfolio_overlap = AsyncMock(return_value={})
    candidate = {
        "provider": "yahoo", "provider_id": "SAP.DE", "symbol": "SAP.DE",
        "name": "Synthetic equity fixture", "category": "stock", "currency": "EUR",
        "exchange": "Xetra",
    }
    return SimpleNamespace(
        manager=manager, modules=modules, clock=clock, quote=quote_payload,
        histories=history_payloads, candidate=candidate,
    )


def _indication(env, **kwargs):
    options = {
        "candidates": [deepcopy(env.candidate)], "scope": "search", "amount": 1000,
        "overlap_policy": "allow", "risk_tolerance": "very_high", "horizon": "medium",
        "execution_costs": {"confirmed": True, "fixed_fee": 0, "commission_pct": 0, "spread_bps": 0, "fx_bps": 0},
    }
    options.update(kwargs)
    return asyncio.run(env.manager.async_indication("fixture-user", **options))


def _assert_abstains(result, *, reason_fragment=None):
    assert result["allocation"]["deployed"] == 0
    assert result["allocation"]["cash_reserve"] == 1000
    visible = [*(result.get("results") or []), *(result.get("excluded_candidates") or [])]
    assert visible, "Unusable evidence must have a visible candidate or exclusion explanation"
    for row in result.get("results") or []:
        assert row["suggested_amount"] == 0
        assert row["suggested_units"] == 0
        assert row["allocation_eligible"] is False
    if reason_fragment:
        assert reason_fragment in repr(visible).lower()


def test_positive_control_real_provider_parser_and_runtime_can_allocate(environment):
    result = _indication(environment)
    assert result["allocation"]["deployed"] > 0
    assert result["results"][0]["allocation_eligible"] is True
    assert result["results"][0]["suggested_amount"] > 0


def test_unconfirmed_costs_block_production_purchases_with_visible_reason(environment):
    result = _indication(environment, execution_costs=None)
    _assert_abstains(result)
    assert result["allocation"]["execution_costs"]["status"] == "unknown"
    assert "execution_costs_unknown" in result["allocation"]["portfolio_risk"]["blockers"]
    assert result["preferences"]["execution_costs"] is None


@pytest.mark.parametrize("mode", ["deterministic", "full_ai"])
@pytest.mark.parametrize("whole", [False, True])
def test_production_final_plan_preserves_fees_cash_and_optional_risk_inputs(environment, mode, whole):
    environment.manager._ai_indication_review = AsyncMock(return_value={"structured": {"ranking": []}})
    costs = {"confirmed": True, "fixed_fee": 2., "commission_pct": .25, "spread_bps": 12., "fx_bps": 10.}
    result = _indication(
        environment, mode=mode, whole_units_only=whole, execution_costs=costs,
        existing_cash=123., max_drawdown_pct=20., analysis_horizon_weeks=52,
    )
    allocation = result["ai_allocation" if mode == "full_ai" else "allocation"]
    summary = allocation["execution_costs"]
    portfolio = allocation["portfolio_risk"]
    assert summary["purchase_count"] == 1
    assert summary["estimated_transaction_cost"] == pytest.approx(2 + summary["principal"] * .0047 + summary["rounding_allowance"], abs=1e-9)
    assert summary["principal"] + summary["estimated_transaction_cost"] <= summary["estimated_cash_debit"] + 1e-8
    assert summary["estimated_cash_debit"] <= 1000
    assert allocation["cash_reserve"] == pytest.approx(1000 - summary["estimated_cash_debit"])
    assert portfolio["portfolio_value_before"] == 1123
    assert portfolio["before_purchases"]["cash"] == 1123
    assert portfolio["cash_after"] == pytest.approx(123 + allocation["cash_reserve"])
    assert portfolio["portfolio_value_after"] == pytest.approx(1123 - summary["estimated_transaction_cost"])
    assert portfolio["limits"]["max_drawdown_3y"] == .2
    assert portfolio["evidence"]["horizon"]["horizon_weeks"] == 52
    stored = environment.manager.store.async_set_preferences.call_args.kwargs["indication_preferences"]
    for key in ("execution_costs", "existing_cash", "max_drawdown_pct", "analysis_horizon_weeks"):
        assert result["preferences"][key] == stored[key]
    if whole:
        row = result["results"][0]
        unit_key = "ai_suggested_units" if mode == "full_ai" else "suggested_units"
        assert row[unit_key] == math.floor(row[unit_key])
        assert row["whole_unit_selected"] is (row[unit_key] >= 1)
    json.dumps(result, allow_nan=False)


def _portfolio_with(environment, holdings):
    # Real storage migrates legacy aggregates to authoritative dated BUY rows.
    # Test holdings mirror that boundary; explicit malformed ledgers are retained.
    for index, holding in enumerate(holdings):
        if "transactions" not in holding:
            quantity = holding.get("personal_quantity", holding.get("quantity"))
            holding["transactions"] = [{"id": f"held-buy-{index}", "type": "buy", "date": "2020-01-02", "quantity": quantity}]
    environment.modules.core.InvestmentManager.async_portfolio.return_value = {
        "holdings": holdings, "categories": [], "total": sum(h.get("value", 0) for h in holdings),
    }
    environment.manager.store.async_user.return_value["holdings"] = deepcopy(holdings)


def test_display_timeout_cannot_hide_positive_authoritative_ledger_quantity(environment):
    _portfolio_with(environment, [{
        **environment.candidate, "quantity": 0., "value": 0., "status": "error",
        "transactions": [{"id": "positive-ledger-buy", "type": "buy", "date": "2026-01-02", "quantity": 100.}],
    }])
    result = _indication(environment)
    portfolio = result["allocation"]["portfolio_risk"]
    assert portfolio["existing_value"] == pytest.approx(100 * environment.quote["meta"]["regularMarketPrice"])
    assert portfolio["portfolio_value_before"] > 10000
    assert "sleeve:single_equity" in portfolio["baseline_breaches"]
    assert result["allocation"]["deployed"] < 1e-6


@pytest.mark.parametrize("transactions", [None, [], [{"id": "bad", "type": "buy", "date": "2020-01-02", "quantity": math.nan}]])
def test_missing_or_invalid_ledger_cannot_be_certified_from_zero_display_quantity(environment, transactions):
    _portfolio_with(environment, [{**environment.candidate, "quantity": 0., "status": "error", "transactions": transactions}])
    result = _indication(environment)
    _assert_abstains(result)
    portfolio = result["allocation"]["portfolio_risk"]
    assert portfolio["status"] == "unknown"
    assert portfolio["existing_value"] is None
    assert portfolio["existing_position_issues"][0]["symbol"] == environment.candidate["symbol"]
    assert portfolio["existing_position_issues"][0]["reasons"]


def test_fully_shared_buy_is_verified_zero_personal_exposure(environment):
    _portfolio_with(environment, [{
        **environment.candidate, "quantity": 100., "value": 10000.,
        "transactions": [{"id": "fully-shared-buy", "type": "buy", "date": "2020-01-02", "quantity": 100.,
                          "shared_allocations": [{"participant": "Participant fixture", "quantity": 100.}]}],
    }])
    result = _indication(environment)
    portfolio = result["allocation"]["portfolio_risk"]
    assert portfolio["existing_value"] == 0
    assert portfolio["existing_position_issues"] == []
    assert result["allocation"]["deployed"] > 0


def test_complete_portfolio_reuses_fresh_candidate_quote_and_personal_ledger_quantity(environment):
    _portfolio_with(environment, [{**environment.candidate, "personal_quantity": 2., "quantity": 1000., "value": 9000.}])
    result = _indication(environment, existing_cash=100.)
    portfolio = result["allocation"]["portfolio_risk"]
    assert portfolio["existing_value"] == pytest.approx(2 * result["results"][0]["portfolio_price"])
    assert portfolio["portfolio_value_before"] == pytest.approx(portfolio["existing_value"] + 1100)
    requests = [call.args[1:3] for call in environment.manager.yahoo._chart.await_args_list]
    assert requests.count(("5d", "1d")) == 1
    assert requests.count(("1y", "1d")) == 1
    assert requests.count(("5y", "1d")) == 1


def test_held_instrument_outside_purchase_candidates_is_fetched_and_checked(environment, monkeypatch):
    held = {**environment.candidate, "provider_id": "SXR8.DE", "symbol": "SXR8.DE", "name": "iShares Core S&P 500 UCITS ETF USD Accumulating", "category": "etf", "quantity": 2., "value": 200.}
    _portfolio_with(environment, [held])
    original = environment.manager.yahoo._chart.side_effect

    async def chart(provider_id, range_, interval, **kwargs):
        payload = await original(provider_id, range_, interval, **kwargs)
        payload["meta"].update(symbol=provider_id, instrumentType="ETF" if provider_id == "SXR8.DE" else "EQUITY")
        return payload

    environment.manager.yahoo._chart.side_effect = chart
    original_plan = environment.modules.core.finalize_purchase_plan
    checked_positions = []

    def checked_plan(*args, **kwargs):
        checked_positions.extend(kwargs.get("existing_positions", []))
        return original_plan(*args, **kwargs)

    monkeypatch.setattr(environment.modules.core, "finalize_purchase_plan", checked_plan)
    result = _indication(environment, existing_instruments="exclude")
    assert [row["symbol"] for row in result["results"]] == ["SAP.DE"]
    portfolio = result["allocation"]["portfolio_risk"]
    assert portfolio["existing_value"] == pytest.approx(2 * environment.quote["meta"]["regularMarketPrice"]), [(p.get("symbol"), p.get("input_evidence_blockers"), p.get("metrics", {}).get("input_evidence_error")) for p in checked_positions]
    assert portfolio["status"] == "within_limits"
    assert any(call.args[0] == "SXR8.DE" and call.args[1:3] == ("5y", "1d") for call in environment.manager.yahoo._chart.await_args_list)


@pytest.mark.parametrize("quantity", [None, True, math.nan, -1])
def test_invalid_held_quantity_reaches_complete_portfolio_gate(environment, quantity):
    _portfolio_with(environment, [{**environment.candidate, "quantity": quantity, "value": 10.}])
    result = _indication(environment)
    _assert_abstains(result)
    portfolio = result["allocation"]["portfolio_risk"]
    assert portfolio["status"] == "unknown"
    assert portfolio["existing_value"] is None
    assert "existing_position_value_unavailable" in portfolio["blockers"]


def test_unverified_held_fund_blocks_even_when_excluded_from_candidate_set(environment):
    held = {**environment.candidate, "provider_id": "UNKNOWN.DE", "symbol": "UNKNOWN.DE", "category": "etf", "quantity": 1., "value": 100.}
    _portfolio_with(environment, [held])
    result = _indication(environment, existing_instruments="exclude")
    _assert_abstains(result)
    assert result["allocation"]["portfolio_risk"]["status"] == "unknown"
    assert result["allocation"]["portfolio_risk"]["scope"] == "complete_portfolio"
    assert result["allocation"]["portfolio_risk"]["post"] is None


def test_explicit_ignored_portfolio_has_distinct_scope_and_excludes_other_cash(environment):
    _portfolio_with(environment, [{**environment.candidate, "quantity": None, "value": 10.}])
    result = _indication(environment, portfolio_context="ignore", existing_cash=500.)
    assert result["allocation"]["deployed"] > 0
    portfolio = result["allocation"]["portfolio_risk"]
    assert portfolio["scope"] == "new_contribution_only"
    assert portfolio["existing_cash"] == 0
    assert portfolio["portfolio_value_before"] == 1000


@pytest.mark.parametrize("mode", ["deterministic_ai", "full_ai"])
@pytest.mark.parametrize("whole", [False, True])
def test_ai_latency_rechecks_freshness_before_returning_final_purchases(environment, mode, whole):
    async def slow_review(*args, **kwargs):
        environment.clock.now += 10 * DAY
        return {"structured": {"ranking": [{**environment.candidate, "score": 100, "action": "buy", "suggested_amount": 100000, "reason": "adversarial"}]}}

    environment.manager._ai_indication_review = AsyncMock(side_effect=slow_review)
    result = _indication(environment, mode=mode, whole_units_only=whole)
    _assert_abstains(result)
    assert result["ai_review"] is None
    if mode == "full_ai":
        assert result["ai_allocation"]["deployed"] == 0
        assert all(row["ai_suggested_amount"] == 0 for row in result["results"])
    if whole:
        assert all(row["whole_unit_selected"] is False for row in result["results"])


@pytest.mark.parametrize("change", ["new_holding", "changed_transaction", "base_currency"])
@pytest.mark.parametrize("mode", ["deterministic_ai", "full_ai"])
def test_concurrent_portfolio_edit_cannot_leave_a_stale_complete_portfolio_plan(environment, change, mode):
    if change == "changed_transaction":
        _portfolio_with(environment, [{**environment.candidate, "quantity": 1., "value": 100.}])

    async def changed_review(*args, **kwargs):
        user = environment.manager.store.async_user.return_value
        if change == "base_currency":
            user["base_currency"] = "USD"
        elif change == "changed_transaction":
            user["holdings"][0]["transactions"][0]["quantity"] = 100.
        else:
            user["holdings"] = [{**environment.candidate, "quantity": 0., "transactions": [
                {"id": "concurrent-buy", "type": "buy", "date": "2026-01-02", "quantity": 100.},
            ]}]
        return {"structured": {"ranking": [{**environment.candidate, "score": 100, "action": "buy", "suggested_amount": 100000}]}}

    environment.manager._ai_indication_review = AsyncMock(side_effect=changed_review)
    result = _indication(environment, mode=mode)
    _assert_abstains(result)
    portfolio = result["allocation"]["portfolio_risk"]
    assert portfolio["ledger_changed_during_analysis"] is True
    assert portfolio["portfolio_snapshot_issue"] == ("portfolio_currency_changed_during_analysis" if change == "base_currency" else "portfolio_ledger_changed_during_analysis")
    assert portfolio["status"] == "unknown"
    assert portfolio["post"] is None
    assert result["ai_review"] is None
    if mode == "full_ai":
        assert result["ai_allocation"]["deployed"] == 0
        assert result["ai_allocation"]["portfolio_risk"]["ledger_changed_during_analysis"] is True


def test_ignored_portfolio_scope_is_explicit_and_does_not_claim_concurrent_ledger_verification(environment):
    async def changed_review(*args, **kwargs):
        environment.manager.store.async_user.return_value["holdings"] = [{"transactions": None}]
        return {"structured": {}}

    environment.manager._ai_indication_review = AsyncMock(side_effect=changed_review)
    result = _indication(environment, mode="deterministic_ai", portfolio_context="ignore")
    assert result["allocation"]["deployed"] > 0
    portfolio = result["allocation"]["portfolio_risk"]
    assert portfolio["scope"] == "new_contribution_only"
    assert portfolio["ledger_changed_during_analysis"] is False


def test_currency_change_blocks_even_when_holdings_are_explicitly_ignored(environment):
    async def changed_review(*args, **kwargs):
        environment.manager.store.async_user.return_value["base_currency"] = "USD"
        return {"structured": {"verdict": "approve", "summary": "This old currency view must not survive"}}

    environment.manager._ai_indication_review = AsyncMock(side_effect=changed_review)
    result = _indication(environment, mode="deterministic_ai", portfolio_context="ignore")
    _assert_abstains(result)
    assert result["ai_review"] is None
    portfolio = result["allocation"]["portfolio_risk"]
    assert portfolio["scope"] == "new_contribution_only"
    assert portfolio["status"] == "unknown"
    assert portfolio["ledger_changed_during_analysis"] is True
    assert portfolio["portfolio_snapshot_issue"] == "portfolio_currency_changed_during_analysis"


@pytest.mark.parametrize("malformed", [None, False, {}])
@pytest.mark.parametrize("stage", ["initial", "final"])
def test_unknown_holding_membership_cannot_compare_equal_as_an_empty_book(environment, malformed, stage):
    if stage == "initial":
        environment.modules.core.InvestmentManager.async_portfolio.return_value["holdings"] = malformed

    async def review(*args, **kwargs):
        if stage == "final":
            environment.manager.store.async_user.return_value["holdings"] = malformed
        return {"structured": {"verdict": "approve"}}

    environment.manager._ai_indication_review = AsyncMock(side_effect=review)
    result = _indication(environment, mode="deterministic_ai")
    _assert_abstains(result)
    assert result["ai_review"] is None
    portfolio = result["allocation"]["portfolio_risk"]
    assert portfolio["status"] == "unknown"
    assert portfolio["portfolio_snapshot_issue"] == "portfolio_ledger_snapshot_unavailable"


@pytest.mark.parametrize("field,bad", [
    ("existing_cash", True), ("existing_cash", math.inf), ("existing_cash", -1),
    ("max_drawdown_pct", True), ("max_drawdown_pct", 0), ("max_drawdown_pct", 101), ("max_drawdown_pct", math.nan),
    ("analysis_horizon_weeks", True), ("analysis_horizon_weeks", 0), ("analysis_horizon_weeks", 1.5), ("analysis_horizon_weeks", 5201),
    ("execution_costs", {"confirmed": True, "fixed_fee": None, "commission_pct": 0, "spread_bps": 0, "fx_bps": 0}),
])
def test_invalid_new_preferences_fail_before_persistence_or_market_fetch(environment, field, bad):
    with pytest.raises(ValueError):
        _indication(environment, **{field: bad})
    environment.manager.store.async_set_preferences.assert_not_called()
    environment.manager.yahoo._chart.assert_not_called()


def test_long_signals_and_daily_risk_keep_distinct_provider_provenance(environment):
    result = _indication(environment, horizon="long")
    row = result["results"][0]
    assert row["allocation_eligible"] is True
    assert row["metrics"]["risk_history_source_period"] == "5y_risk"
    assert row["metrics"]["validated_risk_history_source"].endswith(":daily_sessions")
    assert row["data_quality"]["signal"]["cadence"] == "weekly"
    assert row["data_quality"]["signal"]["observations"] == 220
    assert row["data_quality"]["risk"]["cadence"] == "daily"
    assert row["data_quality"]["risk"]["observations"] == 1000
    requests = [call.args[1:3] for call in environment.manager.yahoo._chart.await_args_list]
    assert requests.count(("5y", "1wk")) == 1
    assert requests.count(("5y", "1d")) == 1


def test_daily_risk_rejects_weekly_response_even_when_weekly_signal_cache_is_valid(environment):
    holding = {**environment.candidate, "_observed_exchange": "GER"}
    weekly, _ = asyncio.run(environment.manager._validated_indication_history(holding, "5y", "EUR"))
    assert len(weekly) == 220
    # A valid cached weekly series must not hide an endpoint serving the wrong
    # bar interval for the independently requested raw daily risk observations.
    environment.histories["5y_risk"]["meta"]["dataGranularity"] = "1wk"
    _assert_abstains(_indication(environment))
    requests = [call.args[1:3] for call in environment.manager.yahoo._chart.await_args_list]
    assert requests.count(("5y", "1wk")) == 1  # The explicit cache priming only.
    assert requests.count(("5y", "1d")) == 1


def test_daily_risk_local_session_dates_survive_history_cache_and_fx(environment):
    stamps = [
        int(datetime(2026, 10, 4, 22, 30, tzinfo=timezone.utc).timestamp()),
        int(datetime(2026, 10, 5, 22, 30, tzinfo=timezone.utc).timestamp()),
    ]
    payload = environment.histories["5y_risk"]
    payload["timestamp"] = stamps
    payload["indicators"]["quote"][0]["close"] = [100.0, 101.0]
    payload["indicators"]["adjclose"][0]["adjclose"] = [100.0, 101.0]
    holding = {**environment.candidate, "_observed_exchange": "GER"}
    points, source = asyncio.run(environment.manager._validated_indication_history(holding, "5y_risk", "EUR"))
    expected_dates = ["2026-10-05", "2026-10-06"]
    assert [point.ts for point in points] == stamps
    assert [point.session_date for point in points] == expected_dates
    calls = environment.manager.yahoo._chart.call_count
    cached, cached_source = asyncio.run(environment.manager._validated_indication_history(holding, "5y_risk", "EUR"))
    assert environment.manager.yahoo._chart.call_count == calls
    assert cached_source == source
    assert [point.as_dict() for point in cached] == [point.as_dict() for point in points]

    same_currency = asyncio.run(environment.manager._convert_history(
        cached, "EUR", "EUR", "5y_risk", require_fresh=True,
    ))
    assert [point.as_dict() for point in same_currency] == [point.as_dict() for point in points]
    point = environment.modules.models.HistoryPoint
    environment.manager.frankfurter.async_history.return_value = [
        point(int(datetime(2026, 10, 2, tzinfo=timezone.utc).timestamp()), 1.1),
        point(int(datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp()), 1.2),
    ]
    converted = asyncio.run(environment.manager._convert_history(
        cached, "EUR", "USD", "5y_risk", require_fresh=True,
    ))
    assert [point.ts for point in converted] == stamps
    assert [point.session_date for point in converted] == expected_dates
    assert [point.value for point in converted] == pytest.approx([110.0, 121.2])


@pytest.mark.parametrize("stamp", [None, NOW - 40 * DAY, NOW + DAY])
@pytest.mark.parametrize("whole", [False, True])
def test_unusable_quote_evidence_cannot_reach_any_execution_mode(environment, stamp, whole):
    environment.quote["meta"]["regularMarketTime"] = stamp
    _assert_abstains(_indication(environment, whole_units_only=whole))


@pytest.mark.parametrize("period", ["1y", "5y_risk"])
@pytest.mark.parametrize("problem", ["stale", "future", "duplicate", "boolean"])
def test_raw_history_evidence_is_checked_before_values_or_week_buckets_erase_it(environment, period, problem):
    payload = environment.histories[period]
    if problem == "stale":
        payload["timestamp"] = [stamp - 60 * DAY for stamp in payload["timestamp"]]
    elif problem == "future":
        payload["timestamp"][-1] = NOW + DAY
    elif problem == "duplicate":
        payload["timestamp"][-2] = payload["timestamp"][-1]
    else:
        payload["indicators"]["adjclose"][0]["adjclose"][-1] = True
    _assert_abstains(_indication(environment))


@pytest.mark.parametrize("field,bad", [
    ("symbol", "SAP"), ("currency", "USD"), ("exchangeName", "NYQ"),
])
def test_quote_response_cannot_substitute_a_different_listing(environment, field, bad):
    environment.quote["meta"][field] = bad
    if field == "exchangeName":
        environment.quote["meta"]["fullExchangeName"] = "NYSE"
    _assert_abstains(_indication(environment))


@pytest.mark.parametrize("period", ["1y", "5y_risk"])
@pytest.mark.parametrize("field,bad", [
    ("symbol", "SAP"), ("currency", "USD"), ("exchangeName", "NYQ"),
])
def test_history_response_identity_cannot_borrow_a_correct_quote_identity(environment, period, field, bad):
    environment.histories[period]["meta"][field] = bad
    if field == "exchangeName":
        environment.histories[period]["meta"]["fullExchangeName"] = "NYSE"
    _assert_abstains(_indication(environment))


def test_cached_quote_is_rechecked_against_analysis_clock(environment):
    # Fetching/caching is recent; the trade itself crosses the hard freshness bound.
    first = _indication(environment)
    assert first["allocation"]["deployed"] > 0
    calls = environment.manager.yahoo._chart.call_count
    environment.clock.now += 10 * DAY
    second = _indication(environment)
    assert environment.manager.yahoo._chart.call_count == calls
    _assert_abstains(second)


@pytest.mark.parametrize("whole", [False, True])
def test_full_ai_cannot_reactivate_a_stale_candidate(environment, whole):
    environment.quote["meta"]["regularMarketTime"] = NOW - 40 * DAY
    environment.manager._ai_indication_review = AsyncMock(return_value={
        "structured": {"ranking": [{
            **environment.candidate, "score": 100, "action": "buy",
            "suggested_amount": 100000, "reason": "Synthetic adversarial response",
        }]},
    })
    result = _indication(environment, mode="full_ai", whole_units_only=whole)
    _assert_abstains(result)
    assert result["ai_allocation"]["deployed"] == 0
    assert result["ai_allocation"]["cash_reserve"] == 1000
    assert all(row["ai_suggested_amount"] == 0 for row in result["results"])


def test_exact_fund_identity_survives_scoring_and_final_classifier(environment):
    environment.candidate.update({
        "provider_id": "SXR8.DE", "symbol": "SXR8.DE", "category": "etf",
        "name": "Misleading Overnight Money Market Name",
    })
    for payload in [environment.quote, *environment.histories.values()]:
        payload["meta"].update({"symbol": "SXR8.DE", "instrumentType": "ETF"})
    result = _indication(environment, risk_tolerance="very_low")
    assert result["results"][0]["economic_sleeve"] == "broad_equity"
    assert result["results"][0]["economic_sleeve_cap"] == .2
    assert 0 < result["allocation"]["deployed"] <= 200
    environment.candidate["name"] = "iShares Core S&P 500 UCITS ETF USD Accumulating"
    accurate_name = _indication(environment, risk_tolerance="very_low")
    assert accurate_name["results"][0]["economic_sleeve"] == "broad_equity"
    assert accurate_name["allocation"]["deployed"] == result["allocation"]["deployed"]


@pytest.mark.parametrize("symbol,name,first_week", [
    ("CEMK.DE", "iShares Euro Govt Bond 0-1yr UCITS ETF", (2025, 8, 25)),
    ("VGGF.DE", "Vanguard Global Government Bond EUR Hedged Accumulating", (2025, 3, 24)),
])
def test_partial_week_at_midweek_inception_does_not_disable_a_valid_listing(environment, symbol, name, first_week):
    environment.candidate.update({
        "provider_id": symbol, "symbol": symbol, "category": "etf", "name": name,
    })
    for payload in [environment.quote, *environment.histories.values()]:
        payload["meta"].update({"symbol": symbol, "instrumentType": "ETF"})
    first = int(datetime(*first_week, tzinfo=timezone.utc).timestamp())
    stamps = list(range(first, NOW, 7 * DAY))
    environment.histories["5y"]["timestamp"] = stamps
    environment.histories["5y"]["indicators"]["quote"][0]["close"] = _prices(stamps)
    environment.histories["5y"]["indicators"]["adjclose"][0]["adjclose"] = _prices(stamps)
    result = _indication(environment, horizon="long")
    row = result["results"][0]
    assert row["instrument_identity_eligible"] is True
    assert row["data_quality_eligible"] is True
    assert row["risk_history_eligible"] is True
    assert row["data_quality"]["signal"]["pre_inception_observations_excluded"] == 1
    assert row["data_quality"]["risk"]["pre_inception_observations_excluded"] > 0
    assert "history_predates_exact_share_class" not in row.get("input_evidence_blockers", [])


def test_unknown_wrapper_cannot_self_certify_with_candidate_metadata(environment):
    environment.candidate.update({
        "provider_id": "UNKNOWN.DE", "symbol": "UNKNOWN.DE", "category": "etf",
        "name": "Synthetic Treasury Overnight Money Market ETF",
        "isin": "IE00B5BMR087", "instrument_identity_status": "verified",
        "instrument_identity_eligible": True, "data_quality_eligible": True,
        "economic_sleeve": "cash_like", "product_structure_flags": [],
    })
    for payload in [environment.quote, *environment.histories.values()]:
        payload["meta"].update({"symbol": "UNKNOWN.DE", "instrumentType": "ETF"})
    _assert_abstains(_indication(environment, risk_tolerance="very_low"))


@pytest.mark.parametrize("field,value", [
    ("leveraged", True), ("leverage_factor", 2.0), ("high_yield", True),
    ("income_treatment", "distributing"),
])
def test_candidate_sanitizer_retains_contradictory_structure_assertions(environment, field, value):
    environment.candidate.update({
        "provider_id": "SXR8.DE", "symbol": "SXR8.DE", "category": "etf",
        "name": "iShares Core S&P 500 UCITS ETF", field: value,
    })
    for payload in [environment.quote, *environment.histories.values()]:
        payload["meta"].update({"symbol": "SXR8.DE", "instrumentType": "ETF"})
    _assert_abstains(_indication(environment))


def _crypto_candidate(environment):
    environment.candidate.update({
        "provider_id": "BTC-EUR", "symbol": "BTC/EUR", "category": "crypto",
        "name": "Synthetic crypto fixture", "exchange": "CCC",
    })
    for payload in [environment.quote, *environment.histories.values()]:
        payload["meta"].update({
            "symbol": "BTC-EUR", "instrumentType": "CRYPTOCURRENCY",
            "exchangeName": "CCC", "fullExchangeName": "CCC",
            "exchangeTimezoneName": "UTC",
        })


def test_crypto_positive_control_keeps_unadjusted_but_identified_history(environment):
    _crypto_candidate(environment)
    result = _indication(environment)
    assert result["allocation"]["deployed"] > 0


def test_crypto_raw_boolean_history_is_not_laundered_by_float_conversion(environment):
    _crypto_candidate(environment)
    environment.histories["5y_risk"]["indicators"]["quote"][0]["close"][-1] = True
    _assert_abstains(_indication(environment))


def test_yahoo_missing_market_timestamp_is_never_fetch_time(environment):
    environment.quote["meta"].pop("regularMarketTime")
    quote = asyncio.run(environment.manager.yahoo.async_quote("SAP.DE"))
    assert quote.market_time is None


def test_yahoo_retains_source_timestamp(environment):
    environment.quote["meta"]["regularMarketTime"] = NOW - 40 * DAY
    quote = asyncio.run(environment.manager.yahoo.async_quote("SAP.DE"))
    assert quote.market_time == NOW - 40 * DAY


def test_yahoo_fallback_close_uses_its_own_matching_timestamp(environment):
    environment.quote["meta"].pop("regularMarketPrice")
    environment.quote["meta"]["regularMarketTime"] = NOW
    environment.quote["timestamp"] = [NOW - 4 * DAY, NOW - 3 * DAY, NOW - DAY]
    environment.quote["indicators"]["quote"][0]["close"] = [95.0, 96.0, None]
    quote = asyncio.run(environment.manager.yahoo.async_quote("SAP.DE"))
    assert quote.price == 96
    assert quote.market_time == NOW - 3 * DAY


def test_adjusted_history_cannot_mix_pounds_with_pence(environment):
    environment.histories["1y"]["meta"].update({
        "symbol": "GSK.L", "currency": "GBP", "exchangeName": "LSE",
        "fullExchangeName": "London Stock Exchange",
        "exchangeTimezoneName": "Europe/London",
    })
    with pytest.raises(environment.modules.base.ProviderError):
        asyncio.run(environment.manager.yahoo.async_adjusted_history(
            "GSK.L", "1y", expected_currency="GBp", require_exact_symbol=True,
        ))


def test_history_cache_keeps_pounds_and_pence_as_different_quote_units(environment):
    holding = {
        "provider": "yahoo", "provider_id": "GSK.L", "symbol": "GSK.L", "category": "stock",
        "currency": "GBP", "exchange": "LSE", "_observed_exchange": "LSE",
    }
    payload = environment.histories["1y"]
    payload["meta"].update({
        "symbol": "GSK.L", "currency": "GBP", "exchangeName": "LSE",
        "fullExchangeName": "London Stock Exchange",
        "exchangeTimezoneName": "Europe/London",
    })
    pounds, _ = asyncio.run(environment.manager._validated_indication_history(holding, "1y", "GBP"))
    calls = environment.manager.yahoo._chart.call_count
    payload["meta"]["currency"] = "GBp"
    payload["indicators"]["adjclose"][0]["adjclose"] = [point.value * 100 for point in pounds]
    holding["currency"] = "GBp"
    pence, _ = asyncio.run(environment.manager._validated_indication_history(holding, "1y", "GBp"))
    assert environment.manager.yahoo._chart.call_count == calls + 1
    assert pence[-1].value == pytest.approx(pounds[-1].value * 100)


@pytest.mark.parametrize("exchange,allowed", [("NYSE", False), ("NASDAQ", True)])
def test_paid_history_fallback_requires_same_venue_not_just_symbol_currency(environment, exchange, allowed):
    error = environment.modules.base.ProviderError
    environment.manager.providers["twelve_data"] = SimpleNamespace(
        async_adjusted_history=AsyncMock(side_effect=error("adjusted endpoint unavailable")),
    )
    environment.histories["1y"]["meta"].update({
        "symbol": "AAPL", "currency": "USD", "exchangeName": "NMS", "instrumentType": "EQUITY",
        "fullExchangeName": "NasdaqGS",
        "exchangeTimezoneName": "America/New_York",
    })
    holding = {
        "provider": "twelve_data", "provider_id": f"AAPL|{exchange}|USD",
        "symbol": "AAPL", "category": "stock", "currency": "USD",
        "exchange": exchange, "_observed_exchange": exchange,
    }
    operation = environment.manager._validated_indication_history(holding, "1y", "USD")
    if allowed:
        points, source = asyncio.run(operation)
        assert points
        assert source.startswith("yahoo:")
    else:
        with pytest.raises(error):
            asyncio.run(operation)


def test_alpha_quote_uses_latest_trading_day_not_fetch_time(environment):
    provider = environment.modules.alpha.AlphaVantageProvider(None, "fixture-key")
    provider._get_json = AsyncMock(return_value={"Global Quote": {
        "01. symbol": "SAP", "05. price": "100", "07. latest trading day": "2026-09-01",
    }})
    quote = asyncio.run(provider.async_quote("SAP|USD"))
    assert quote.market_time == int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())


def test_twelve_missing_timestamp_is_never_fetch_time(environment):
    provider = environment.modules.twelve.TwelveDataProvider(None, "fixture-key")
    provider._get_json = AsyncMock(return_value={"symbol": "SAP", "currency": "EUR", "close": "100"})
    quote = asyncio.run(provider.async_quote("SAP|XETRA|EUR"))
    assert quote.market_time is None


def test_twelve_boolean_quote_cannot_become_a_numeric_price(environment):
    provider = environment.modules.twelve.TwelveDataProvider(None, "fixture-key")
    provider._get_json = AsyncMock(return_value={
        "symbol": "BTC/EUR", "currency": "EUR", "exchange": "Binance", "type": "Digital Currency",
        "close": True, "timestamp": NOW - 30,
    })
    with pytest.raises(environment.modules.base.ProviderError):
        asyncio.run(provider.async_quote("BTC/EUR|Binance|EUR"))


@pytest.mark.parametrize("field,bad", [("close", False), ("close", 0), ("timestamp", False), ("timestamp", 0)])
def test_twelve_invalid_present_field_cannot_be_hidden_by_an_alternate_field(environment, field, bad):
    provider = environment.modules.twelve.TwelveDataProvider(None, "fixture-key")
    payload = {
        "symbol": "BTC/EUR", "currency": "EUR", "exchange": "Binance", "type": "Digital Currency",
        "close": "100", "price": "100", "timestamp": NOW - 30,
        "datetime": "2026-10-06 17:59:30",
    }
    payload[field] = bad
    provider._get_json = AsyncMock(return_value=payload)
    with pytest.raises(environment.modules.base.ProviderError):
        asyncio.run(provider.async_quote("BTC/EUR|Binance|EUR"))


@pytest.mark.parametrize("observed_symbol,allowed", [("DOGE/EUR", False), ("BTC/EUR", True)])
def test_twelve_crypto_history_has_its_own_pair_identity(environment, observed_symbol, allowed):
    provider = environment.modules.twelve.TwelveDataProvider(None, "fixture-key")
    provider._get_json = AsyncMock(return_value={
        "meta": {
            "symbol": observed_symbol, "exchange": "Binance", "currency": "EUR",
            "type": "Digital Currency", "exchange_timezone": "UTC",
        },
        "values": [{"datetime": stamp, "close": "100"} for stamp in [NOW - DAY, NOW - 3600]],
    })
    environment.manager.providers["twelve_data"] = provider
    holding = {
        "provider": "twelve_data", "provider_id": "BTC/EUR|Binance|EUR", "symbol": "BTC/EUR",
        "category": "crypto", "currency": "EUR", "exchange": "Binance", "_observed_exchange": "Binance",
        "input_evidence_contract": "source-time-and-exact-identity-v1",
        "instrument_metadata": {
            "provider": "twelve_data", "provider_id": "BTC/EUR|Binance|EUR",
            "symbol": "BTC/EUR", "currency": "EUR", "exchange": "Binance", "category": "crypto",
        },
    }
    operation = environment.manager._validated_indication_history(holding, "1y", "EUR")
    if allowed:
        points, _ = asyncio.run(operation)
        assert len(points) == 2
    else:
        with pytest.raises(environment.modules.base.ProviderError):
            asyncio.run(operation)


def test_kraken_trade_price_and_timestamp_are_the_same_observation(environment):
    provider = environment.modules.kraken.KrakenProvider(None)
    provider._pair_info = AsyncMock(return_value={"wsname": "XBT/EUR", "altname": "XBTEUR"})

    async def get(path, **params):
        if path == "Ticker":
            return {"XXBTZEUR": {"c": ["61001.0", "1"], "o": "60900.0"}}
        assert path == "Trades"
        return {"XXBTZEUR": [["61002.0", ".1", NOW - 123.5, "b", "m", "", 1]], "last": "cursor"}

    provider._get = AsyncMock(side_effect=get)
    quote = asyncio.run(provider.async_quote("XXBTZEUR"))
    assert quote.price == 61002
    assert quote.market_time == NOW - 123.5
    assert {call.args[0] for call in provider._get.await_args_list} == {"Ticker", "Trades"}


@pytest.mark.parametrize("response", [{"XXBTZEUR": [], "last": "cursor"}, {
    "XETHZEUR": [["2000", "1", NOW - 30, "b", "m", "", 1]], "last": "cursor",
}])
def test_kraken_absent_or_wrong_pair_trade_cannot_refresh_ticker_time(environment, response):
    provider = environment.modules.kraken.KrakenProvider(None)
    provider._pair_info = AsyncMock(return_value={"wsname": "XBT/EUR", "altname": "XBTEUR"})

    async def get(path, **params):
        if path == "Ticker":
            return {"XXBTZEUR": {"c": ["61001.0", "1"], "o": "60900.0"}}
        assert path == "Trades"
        return deepcopy(response)

    provider._get = AsyncMock(side_effect=get)
    try:
        quote = asyncio.run(provider.async_quote("XXBTZEUR"))
    except environment.modules.base.ProviderError:
        return
    assert quote.market_time is None
    assert quote.price == 61001


@pytest.mark.parametrize("kind", ["empty", "stale", "future"])
def test_strict_historical_fx_rejects_unusable_rates(environment, kind):
    point = environment.modules.models.HistoryPoint
    source = [point(NOW - DAY, 100.0), point(NOW - 3600, 101.0)]
    fx = {
        "empty": [],
        "stale": [point(NOW - 90 * DAY, .9), point(NOW - 83 * DAY, .91)],
        "future": [point(NOW + DAY, .9), point(NOW + 2 * DAY, .91)],
    }[kind]
    environment.manager.frankfurter.async_history.return_value = fx
    with pytest.raises(environment.modules.base.ProviderError):
        asyncio.run(environment.manager._convert_history(source, "USD", "EUR", "5y_risk", require_fresh=True))


def test_strict_historical_fx_never_backfills_before_first_observation(environment):
    point = environment.modules.models.HistoryPoint
    source = [point(NOW - 7 * DAY, 100.0), point(NOW - 3600, 101.0)]
    environment.manager.frankfurter.async_history.return_value = [
        point(NOW - 2 * DAY, .9), point(NOW - DAY, .91),
    ]
    try:
        converted = asyncio.run(environment.manager._convert_history(
            source, "USD", "EUR", "5y_risk", require_fresh=True,
        ))
    except environment.modules.base.ProviderError:
        return  # Rejecting inadequate alignment is also safe.
    assert converted
    assert min(value.ts for value in converted) >= NOW - 2 * DAY


def test_strict_historical_fx_positive_control_keeps_observed_conversion(environment):
    point = environment.modules.models.HistoryPoint
    source = [point(NOW - DAY, 100.0), point(NOW - 3600, 101.0)]
    environment.manager.frankfurter.async_history.return_value = [
        point(NOW - 2 * DAY, .9), point(NOW - DAY, .91),
    ]
    converted = asyncio.run(environment.manager._convert_history(
        source, "USD", "EUR", "5y_risk", require_fresh=True,
    ))
    assert [(value.ts, value.value) for value in converted] == [
        (source[0].ts, 91.0), (source[1].ts, pytest.approx(91.91)),
    ]


def test_current_fx_boolean_payload_cannot_become_an_identity_rate(environment):
    provider = environment.modules.core.FrankfurterProvider(None)
    provider._get = AsyncMock(return_value={"rate": True, "date": "2026-10-06"})
    environment.manager.frankfurter = provider
    with pytest.raises(environment.modules.base.ProviderError):
        asyncio.run(environment.manager._validated_indication_fx_rate("USD", "EUR"))


def test_quote_and_history_are_rechecked_after_waiting_for_fx(environment):
    environment.candidate.update({
        "provider_id": "MSFT", "symbol": "MSFT", "currency": "USD", "exchange": "NASDAQ",
    })
    for payload in [environment.quote, *environment.histories.values()]:
        payload["meta"].update({
            "symbol": "MSFT", "currency": "USD", "exchangeName": "NMS",
            "fullExchangeName": "NasdaqGS",
            "exchangeTimezoneName": "America/New_York",
        })
    point = environment.modules.models.HistoryPoint

    async def fx_history(*args, **kwargs):
        # Advancing a deterministic clock exercises the boundary without sleeping.
        environment.clock.now += 10 * DAY
        first = environment.histories["5y_risk"]["timestamp"][0] - 7 * DAY
        return [point(stamp, .9) for stamp in range(first, environment.clock.now, DAY)]

    async def fx_rate(*args, **kwargs):
        return .9, datetime.fromtimestamp(environment.clock.now, timezone.utc).date().isoformat()

    environment.manager.frankfurter.async_history.side_effect = fx_history
    environment.manager.frankfurter.async_rate.side_effect = fx_rate
    _assert_abstains(_indication(environment))
