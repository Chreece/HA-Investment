"""The real storage normalization boundary must not certify corrupt ownership."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import importlib
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_indication_evidence_integration import _indication, environment, modules
from test_indication_local_ownership import contribution_manager


@pytest.fixture
def real_store(environment, monkeypatch):
    module = importlib.import_module(environment.modules.runtime.__package__ + ".storage")
    monkeypatch.setattr(module, "time", environment.modules.core.time)
    store = object.__new__(module.InvestmentStore)
    store._data = {"users": {"fixture-user": deepcopy(environment.manager.store.async_user.return_value)}}
    store._lock = asyncio.Lock()
    store._store = SimpleNamespace(async_save=AsyncMock())
    environment.manager.store = store
    return store


def _stored_holding(environment, real_store, *, transactions, quantity=99., **fields):
    holding = {
        **environment.candidate, "id": "stored-holding", "quantity": quantity,
        "transactions": deepcopy(transactions), **fields,
    }
    real_store._data["users"]["fixture-user"]["holdings"] = [holding]
    return holding


def _read_evidence(environment, real_store):
    user = asyncio.run(real_store.async_user("fixture-user"))
    positions = importlib.import_module(environment.modules.runtime.__package__ + ".indication_positions")
    result = positions.position_quantity_evidence(
        user["holdings"][0], as_of=datetime.fromtimestamp(environment.clock.now, timezone.utc),
    )
    return user["holdings"][0], result


def _buy(**changes):
    return {"id": "buy", "type": "buy", "date": "2020-01-02", "quantity": 2., **changes}


@pytest.mark.parametrize("quantity", [2, 2.5])
def test_valid_preledger_position_migrates_once_and_survives_restart(environment, real_store, quantity):
    holding = _stored_holding(environment, real_store, transactions=[], quantity=quantity)
    del holding["transactions"]
    real_store._store.async_load = AsyncMock(return_value=deepcopy(real_store._data))

    asyncio.run(real_store.async_load())

    first, evidence = _read_evidence(environment, real_store)

    assert evidence["status"] == "verified"
    assert evidence["quantity"] == quantity
    assert len(first["transactions"]) == 1
    assert first["transactions"][0]["legacy"] is True
    identity = first["transactions"][0]["id"]
    second, repeated = _read_evidence(environment, real_store)
    assert repeated["quantity"] == quantity
    assert second["transactions"][0]["id"] == identity
    # A restart reads the persisted shape, rather than reminting another BUY.
    real_store._store.async_save.assert_awaited_once()
    persisted = deepcopy(real_store._store.async_save.call_args.args[0])
    restarted = object.__new__(type(real_store))
    restarted._data = {"users": {}}
    restarted._lock = asyncio.Lock()
    restarted._store = SimpleNamespace(
        async_load=AsyncMock(return_value=persisted), async_save=AsyncMock(),
    )
    asyncio.run(restarted.async_load())
    third, after_restart = _read_evidence(environment, restarted)
    assert after_restart["quantity"] == quantity
    assert third["transactions"][0]["id"] == identity
    restarted._store.async_save.assert_not_awaited()


@pytest.mark.parametrize("aggregate", [True, False, math.nan, math.inf, -math.inf, "12", -1., None])
def test_invalid_legacy_aggregate_is_not_minted_as_a_verified_buy(environment, real_store, aggregate):
    holding = _stored_holding(environment, real_store, transactions=[], quantity=aggregate)
    del holding["transactions"]

    normalized, evidence = _read_evidence(environment, real_store)

    assert normalized["transactions"] == []
    assert evidence["status"] == "unknown"
    assert evidence["quantity"] is None


@pytest.mark.parametrize("transactions", [[], None, {}, "corrupt", [None], [True], [[]]])
def test_explicit_empty_or_malformed_ledger_is_retained_as_unknown(environment, real_store, transactions):
    _stored_holding(environment, real_store, transactions=transactions, quantity=1000.)

    normalized, evidence = _read_evidence(environment, real_store)

    assert normalized["transactions"] == transactions
    assert evidence["status"] == "unknown"
    assert evidence["quantity"] is None


@pytest.mark.parametrize("quantity", [True, False, math.nan, math.inf, "2", -1., {}, []])
def test_storage_cannot_normalize_invalid_transaction_units_into_valid_evidence(
    environment, real_store, quantity,
):
    _stored_holding(environment, real_store, transactions=[_buy(quantity=quantity)])

    _, evidence = _read_evidence(environment, real_store)

    assert evidence["status"] == "unknown"
    assert evidence["quantity"] is None


@pytest.mark.parametrize("tx_type", ["transfer", "BUY", True, None, ""])
def test_explicit_unsupported_transaction_type_does_not_become_a_buy(environment, real_store, tx_type):
    _stored_holding(environment, real_store, transactions=[_buy(type=tx_type)])

    normalized, evidence = _read_evidence(environment, real_store)

    assert normalized["transactions"][0]["type"] == tx_type
    assert evidence["status"] == "unknown"
    assert "position_ledger_transaction_type_invalid" in evidence["reasons"]


@pytest.mark.parametrize("day", [None, "", False])
def test_explicit_invalid_date_is_not_replaced_with_a_current_date(environment, real_store, day):
    _stored_holding(environment, real_store, transactions=[_buy(date=day)])

    normalized, evidence = _read_evidence(environment, real_store)

    assert normalized["transactions"][0]["date"] is day
    assert evidence["status"] == "unknown"


def test_legitimate_legacy_transaction_missing_type_and_date_remains_supported(environment, real_store):
    old = _buy(created_at=1577923200)
    del old["type"], old["date"]
    _stored_holding(environment, real_store, transactions=[old], quantity=1000.)

    normalized, evidence = _read_evidence(environment, real_store)

    assert normalized["transactions"][0]["type"] == "buy"
    assert normalized["transactions"][0]["date"] == "2020-01-02"
    assert evidence["status"] == "verified"
    assert evidence["quantity"] == 2.


def test_unrelated_malformed_ledger_does_not_block_a_valid_new_contribution(environment, real_store):
    manager = contribution_manager(environment)
    _stored_holding(
        environment, real_store, transactions=[None], quantity=1000.,
        provider_id="UNRELATED.DE", symbol="UNRELATED.DE",
    )

    result = _indication(environment, portfolio_context="use")

    assert result["risk_scope"] == "new_contribution_only"
    assert result["allocation"]["deployed"] > 0
    assert all(call.args[0] == "SAP.DE" for call in manager.yahoo._chart.await_args_list)
    # Repeated preference/account reads preserve the unverified record.
    assert real_store._data["users"]["fixture-user"]["holdings"][0]["transactions"] == [None]


@pytest.mark.parametrize("transactions", [[], None, [_buy(type="transfer")], [_buy(quantity=True)]])
def test_matching_unknown_ownership_is_blocked_through_real_store(environment, real_store, transactions):
    manager = contribution_manager(environment)
    _stored_holding(environment, real_store, transactions=transactions, quantity=1000.)

    with pytest.raises(ValueError, match="(?i)ownership|verified|verify|unknown|ledger|instruments"):
        _indication(environment, portfolio_context="use")

    manager.yahoo._chart.assert_not_awaited()


@pytest.mark.parametrize("currency", [True, False, 1, math.nan, {}, {"code": "EUR"}, [], ["EUR"], None, "", " "])
def test_invalid_supplied_currency_cannot_be_stringified_into_verified_nonownership(
    environment, real_store, currency,
):
    manager = contribution_manager(environment)
    _stored_holding(environment, real_store, transactions=[_buy()], currency=currency)

    user = asyncio.run(real_store.async_user("fixture-user"))
    preserved = user["holdings"][0]["currency"]
    assert type(preserved) is type(currency)
    if isinstance(currency, float) and math.isnan(currency):
        assert math.isnan(preserved)
    else:
        assert preserved == currency
    ownership = importlib.import_module(environment.modules.runtime.__package__ + ".indication_ownership")
    snapshot = manager._ownership_snapshot(user["holdings"])
    assert ownership.candidate_ownership_evidence(environment.candidate, snapshot)["status"] != "not_owned"

    with pytest.raises(ValueError, match="(?i)ownership|verified|verify|unknown|ledger|instruments"):
        _indication(environment, portfolio_context="use")

    manager.yahoo._chart.assert_not_awaited()


@pytest.mark.parametrize("average", ["corrupt", True, math.nan, {}, []])
def test_bad_legacy_average_does_not_override_units_or_block_an_unrelated_contribution(
    environment, real_store, average,
):
    manager = contribution_manager(environment)
    holding = _stored_holding(
        environment, real_store, transactions=[], quantity=2.5,
        provider_id="UNRELATED.DE", symbol="UNRELATED.DE", average_buy_price=average,
    )
    del holding["transactions"]

    result = _indication(environment, portfolio_context="use")

    assert result["risk_scope"] == "new_contribution_only"
    assert result["allocation"]["deployed"] > 0
    assert all(call.args[0] == "SAP.DE" for call in manager.yahoo._chart.await_args_list)
    stored = real_store._data["users"]["fixture-user"]["holdings"][0]
    preserved = stored["average_buy_price"]
    assert type(preserved) is type(average)
    if isinstance(average, float) and math.isnan(average):
        assert math.isnan(preserved)
    else:
        assert preserved == average
    assert len(stored["transactions"]) == 1
    buy = stored["transactions"][0]
    assert buy["legacy"] is True
    assert buy["quantity"] == buy["net_quantity"] == buy["gross_quantity"] == 2.5
    assert all(buy[field] is None for field in (
        "buy_price", "gross_trade_total", "investment_total", "all_in_total",
    ))
