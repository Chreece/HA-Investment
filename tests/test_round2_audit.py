"""Adversarial checks at the shipped contribution manager's final await boundary."""
from copy import deepcopy
from decimal import Decimal, ROUND_CEILING
from unittest.mock import AsyncMock

import pytest

from test_indication_evidence_integration import (
    DAY,
    _indication,
    environment,
    modules,
)
from test_indication_local_ownership import contribution_manager


def _review(environment):
    environment.manager._ai_indication_review = AsyncMock(return_value={
        "structured": {
            "verdict": "approve", "summary": "Review before the final storage write",
            "ranking": [{**environment.candidate, "score": 100, "action": "buy",
                         "suggested_amount": 100000, "reason": "Synthetic ranking"}],
        },
    })


def _assert_closed(result, mode):
    assert result["allocation"]["deployed"] == 0
    assert result["allocation"]["cash_reserve"] == 1000
    assert result["ai_review"] is None
    assert all(row["suggested_amount"] == row["suggested_units"] == 0
               for row in result["results"])
    if mode == "full_ai":
        assert result["ai_allocation"]["deployed"] == 0
        assert result["ai_allocation"]["cash_reserve"] == 1000
        assert all(row["ai_suggested_amount"] == row["ai_suggested_units"] == 0
                   for row in result["results"])


@pytest.mark.parametrize("mode", ["deterministic", "deterministic_ai", "full_ai"])
@pytest.mark.parametrize("portfolio_context", ["use", "ignore"])
def test_late_preference_write_cannot_return_a_plan_in_an_obsolete_account_currency(
    environment, portfolio_context, mode,
):
    manager = contribution_manager(environment)
    _review(environment)
    calls = 0

    async def persist_preferences(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            manager.store.async_user.return_value["base_currency"] = "USD"

    manager.store.async_set_preferences.side_effect = persist_preferences

    result = _indication(environment, portfolio_context=portfolio_context, mode=mode)
    assert calls == 2
    _assert_closed(result, mode)
    assert result["allocation"]["portfolio_risk"]["portfolio_snapshot_issue"] == (
        "portfolio_currency_changed_during_analysis"
    )


@pytest.mark.parametrize("mode", ["deterministic", "deterministic_ai", "full_ai"])
@pytest.mark.parametrize("portfolio_context", ["use", "ignore"])
def test_late_preference_write_cannot_return_a_purchase_from_expired_source_evidence(
    environment, portfolio_context, mode,
):
    manager = contribution_manager(environment)
    _review(environment)
    calls = 0

    async def persist_preferences(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            environment.clock.now += 10 * DAY

    manager.store.async_set_preferences.side_effect = persist_preferences

    result = _indication(environment, portfolio_context=portfolio_context, mode=mode)
    assert calls == 2
    _assert_closed(result, mode)
    assert "stale" in repr(result["results"]).lower()


@pytest.mark.parametrize("failure", ["currency", "stale_source", "snapshot_unavailable"])
@pytest.mark.parametrize("mode", ["deterministic", "deterministic_ai", "full_ai"])
@pytest.mark.parametrize("portfolio_context", ["use", "ignore"])
def test_final_account_read_keeps_global_vetoes_after_all_persistence(
    environment, portfolio_context, mode, failure,
):
    manager = contribution_manager(environment)
    _review(environment)
    user = manager.store.async_user.return_value
    preference_calls = 0
    final_reads = 0

    async def persist_preferences(*args, **kwargs):
        nonlocal preference_calls
        preference_calls += 1

    async def read_user(*args, **kwargs):
        nonlocal final_reads
        if preference_calls >= 2:
            final_reads += 1
            if failure == "snapshot_unavailable":
                raise RuntimeError("Synthetic unavailable final account snapshot")
            if failure == "currency":
                user["base_currency"] = "USD"
            else:
                environment.clock.now += 10 * DAY
        return user

    manager.store.async_set_preferences.side_effect = persist_preferences
    manager.store.async_user.side_effect = read_user

    result = _indication(environment, portfolio_context=portfolio_context, mode=mode)
    assert preference_calls == 2
    assert final_reads == 1
    _assert_closed(result, mode)
    if failure == "stale_source":
        assert "stale" in repr(result["results"]).lower()
    else:
        assert result["allocation"]["portfolio_risk"]["portfolio_snapshot_issue"] == (
            "portfolio_currency_changed_during_analysis"
            if failure == "currency" else "portfolio_ledger_snapshot_unavailable"
        )


@pytest.mark.parametrize("price,budget,fee", [(4.914, 100., 60.), (4.911, 50., 0.)])
def test_full_ai_preserves_raw_priced_whole_units_when_accepting_the_approved_plan(
    environment, price, budget, fee,
):
    manager = contribution_manager(environment)
    scale = price / environment.quote["meta"]["regularMarketPrice"]
    for payload in [environment.quote, *environment.histories.values()]:
        payload["meta"]["regularMarketPrice"] = price
        for kind, field in (("quote", "close"), ("adjclose", "adjclose")):
            for series in payload["indicators"].get(kind, []):
                series[field] = [value * scale for value in series[field]]
    approved = []

    async def keep_approved_plan(*args, **kwargs):
        approved.extend(deepcopy(kwargs["results"]))
        return {"structured": {
            "summary": "Keep every approved purchase",
            "ranking": [{
                "provider": row["provider"], "provider_id": row["provider_id"],
                "symbol": row["symbol"], "score": 100, "action": "consider",
                "suggested_amount": kwargs["amount"], "reason": "Synthetic ceiling request",
            } for row in kwargs["results"]],
        }}

    manager._ai_indication_review = AsyncMock(side_effect=keep_approved_plan)

    result = _indication(
        environment, amount=budget, mode="full_ai", whole_units_only=True,
        execution_costs={"confirmed": True, "fixed_fee": fee, "commission_pct": 0.,
                         "spread_bps": 0., "fx_bps": 0.},
    )

    assert len(approved) == len(result["results"]) == 1
    assert approved[0]["suggested_units"] >= 1
    row = result["results"][0]
    assert row["ai_suggested_units"] == approved[0]["suggested_units"]
    principal = Decimal(str(price)) * Decimal(str(row["ai_suggested_units"]))
    assert row["ai_suggested_amount"] == pytest.approx(float(principal))
    expected_debit = (principal + Decimal(str(fee))).quantize(Decimal(".01"), rounding=ROUND_CEILING)
    assert Decimal(str(result["ai_allocation"]["execution_costs"]["estimated_cash_debit"])) == expected_debit
    assert row["ai_suggested_amount"] <= approved[0]["suggested_amount"]
