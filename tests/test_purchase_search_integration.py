"""Exercise repeated finalization through the real provider/parser/manager path.

The earlier model's approved projection is the explicit fixture boundary. Its
amounts make fee and float edges reproducible; all later cash, identity, source,
portfolio, AI-ceiling and final response checks execute production code.
"""
from copy import deepcopy
import asyncio
import importlib
import threading
from unittest.mock import AsyncMock

import pytest

from test_indication_evidence_integration import environment, modules, _indication


def approved_projection(monkeypatch, environment, amounts, units):
    def project(rows, weighted, risk, budget, **kwargs):
        projected = deepcopy(rows)
        for row in projected:
            key = row["provider_id"]
            row.update(
                suggested_amount=amounts[key], suggested_units=units[key],
                whole_units_only=True, allocation_eligible=amounts[key] > 0,
            )
        deployed = sum(amounts.values())
        return projected, {
            "deployed": deployed, "cash_reserve": budget - deployed,
            "deployment_fraction": deployed / budget,
        }
    monkeypatch.setattr(environment.modules.core, "production_projection", project)


@pytest.mark.parametrize("mode", ["deterministic", "full_ai"])
@pytest.mark.parametrize("fee", [0., 1.])
def test_live_repeated_finalization_keeps_raw_price_whole_lot_quantity(environment, monkeypatch, mode, fee):
    environment.quote["meta"]["regularMarketPrice"] = 23.252591845784377
    approved_projection(monkeypatch, environment, {"SAP.DE": 100.}, {"SAP.DE": 4.})
    environment.manager._ai_indication_review = AsyncMock(return_value={"structured": {"ranking": []}})
    observed = []
    caller_thread = threading.get_ident()
    real_finalize = environment.modules.core.finalize_purchase_plan

    def finalize(*args, **kwargs):
        rows, meta = real_finalize(*args, **kwargs)
        prefix = "ai_" if kwargs.get("amount_key", "").startswith("ai_") else ""
        observed.append((prefix, rows[0][f"{prefix}suggested_units"],
                         meta["execution_costs"]["estimated_cash_debit"], threading.get_ident()))
        return rows, meta

    monkeypatch.setattr(environment.modules.core, "finalize_purchase_plan", finalize)
    result = _indication(
        environment, amount=100., existing_cash=100., whole_units_only=True,
        max_candidate_pct=100., mode=mode,
        execution_costs={"confirmed": True, "fixed_fee": fee, "commission_pct": 0., "spread_bps": 0., "fx_bps": 0.},
    )
    assert len(observed) == (3 if mode == "full_ai" else 2)
    assert all(units == 4. for _, units, _, _ in observed)
    assert all(debit == 93.02 + fee for _, _, debit, _ in observed)
    assert observed[0][3] != caller_thread
    assert all(thread == caller_thread for _, _, _, thread in observed[1:])
    environment.manager.hass.async_add_executor_job.assert_awaited_once()
    prefix = "ai_" if mode == "full_ai" else ""
    assert result["results"][0][f"{prefix}suggested_units"] == 4.
    summary = result["ai_allocation" if mode == "full_ai" else "allocation"]
    assert summary["execution_costs"]["purchase_count"] == 1
    assert summary["cash_reserve"] == pytest.approx(6.98 - fee)


def fund_environment(environment, symbols=None):
    symbols = symbols or {"XEON.DE": 40., "CEMK.DE": 30., "VAGF.DE": 30.}
    original_chart = environment.manager.yahoo._chart.side_effect

    async def chart(provider_id, range_, interval, **kwargs):
        payload = await original_chart(provider_id, range_, interval, **kwargs)
        payload["meta"].update(symbol=provider_id, instrumentType="ETF")
        if range_ == "5d":
            payload["meta"]["regularMarketPrice"] = symbols[provider_id]
        return payload

    environment.manager.yahoo._chart = AsyncMock(side_effect=chart)
    candidates = [{
        **environment.candidate, "provider_id": symbol, "symbol": symbol,
        "category": "etf", "name": symbol,
    } for symbol in symbols]
    return candidates, symbols


@pytest.mark.parametrize("mode", ["deterministic", "full_ai"])
def test_live_fee_subset_survives_final_recheck_and_preserves_original_search(environment, monkeypatch, mode):
    candidates, prices = fund_environment(environment)
    approved_projection(monkeypatch, environment, prices, dict.fromkeys(prices, 1.))
    environment.manager._ai_indication_review = AsyncMock(return_value={"structured": {"ranking": []}})
    result = _indication(
        environment, candidates=candidates, amount=100., risk_tolerance="medium",
        whole_units_only=True, max_candidate_pct=45., mode=mode,
        execution_costs={"confirmed": True, "fixed_fee": 1., "commission_pct": 0., "spread_bps": 0., "fx_bps": 0.},
    )
    allocation = result["allocation"]
    assert allocation["deployed"] == 70.
    assert allocation["execution_costs"]["estimated_cash_debit"] == 72.
    assert allocation["cash_reserve"] == 28.
    assert allocation["portfolio_risk"]["status"] == "within_limits"
    assert allocation["purchase_search"]["improvement"] is True
    assert allocation["purchase_search"]["initial_principal"] == 0.
    assert allocation["purchase_search"]["selected_principal"] == 70.
    assert allocation["purchase_search"]["whole_candidates"] == 3
    assert allocation["purchase_search"]["optimality_proven"] is True
    assert allocation["purchase_search_verification"]["initial_principal"] == 70.
    assert allocation["purchase_search_verification"]["whole_candidates"] == 2
    active = result["ai_allocation"] if mode == "full_ai" else allocation
    assert active["deployed"] == 70.
    assert active["cash_reserve"] == 28.
    for row in result["results"]:
        assert row["data_quality_eligible"] is True
        assert row["instrument_identity_eligible"] is True
        assert row["suggested_units"] in (0., 1.)
        if mode == "full_ai":
            assert row["ai_suggested_units"] <= row["suggested_units"]


def test_later_source_failure_discards_previous_search_claim(environment, monkeypatch):
    candidates, prices = fund_environment(environment)
    approved_projection(monkeypatch, environment, prices, dict.fromkeys(prices, 1.))

    async def ai_review(*args, **kwargs):
        environment.clock.now += 10 * 86400
        return {"structured": {"ranking": []}}

    environment.manager._ai_indication_review = AsyncMock(side_effect=ai_review)
    result = _indication(
        environment, candidates=candidates, amount=100., risk_tolerance="medium",
        whole_units_only=True, max_candidate_pct=45., mode="full_ai",
        execution_costs={"confirmed": True, "fixed_fee": 1., "commission_pct": 0., "spread_bps": 0., "fx_bps": 0.},
    )
    assert result["allocation"]["deployed"] == 0
    assert result["ai_allocation"]["deployed"] == 0
    assert result["allocation"]["purchase_search"]["improvement"] is False
    assert result["allocation"]["purchase_search"]["status"] in ("not_applicable", "blocked")
    assert result["ai_review"] is None


@pytest.mark.parametrize("mode", ["deterministic", "full_ai"])
def test_recheck_retains_why_no_purchase_was_affordable(environment, monkeypatch, mode):
    approved_projection(monkeypatch, environment, {"SAP.DE": 100.}, {"SAP.DE": 1.})
    environment.manager._ai_indication_review = AsyncMock(return_value={"structured": {"ranking": []}})
    result = _indication(
        environment, amount=100., existing_cash=100., whole_units_only=True,
        mode=mode, max_candidate_pct=100.,
        execution_costs={"confirmed": True, "fixed_fee": 101., "commission_pct": 0., "spread_bps": 0., "fx_bps": 0.},
    )
    allocation = result["allocation"]
    reason = "execution_costs_no_affordable_purchase"
    assert allocation["deployed"] == 0
    assert reason in allocation["execution_costs"]["reasons"]
    assert reason in allocation["portfolio_risk"]["blockers"]
    assert allocation["purchase_search"]["no_positive_feasible"] is True
    assert allocation["purchase_search"]["whole_candidates"] == 1
    assert allocation["purchase_search_verification"]["status"] == "not_applicable"
    assert reason in result["results"][0]["execution_cost_blockers"]
    assert reason in result["results"][0]["portfolio_risk_blockers"]
    if mode == "full_ai":
        assert reason in result["ai_allocation"]["execution_costs"]["reasons"]
        assert reason in result["results"][0]["ai_portfolio_risk_blockers"]


@pytest.mark.parametrize("mode", ["deterministic", "full_ai"])
def test_zero_recheck_does_not_erase_incomplete_original_search(environment, monkeypatch, mode):
    candidates, prices = fund_environment(environment, {"XEON.DE": 60., "VAGF.DE": 20.})
    approved_projection(monkeypatch, environment, prices, dict.fromkeys(prices, 1.))
    search = importlib.import_module(environment.modules.core.__package__ + ".purchase_search")
    monkeypatch.setattr(search, "MAX_SEARCH_RETURN_POINTS", 1)
    environment.manager._ai_indication_review = AsyncMock(return_value={"structured": {"ranking": []}})
    result = _indication(
        environment, candidates=candidates, amount=100., risk_tolerance="medium",
        whole_units_only=True, max_candidate_pct=45., mode=mode,
        execution_costs={"confirmed": True, "fixed_fee": 1., "commission_pct": 0., "spread_bps": 0., "fx_bps": 0.},
    )
    allocation = result["allocation"]
    assert allocation["deployed"] == 0
    assert allocation["purchase_search"]["status"] == "bounded"
    assert allocation["purchase_search"]["no_positive_feasible"] is None
    assert allocation["purchase_search"]["optimality_proven"] is False
    assert allocation["purchase_search"]["whole_candidates"] == 2
    assert allocation["purchase_search_verification"]["status"] == "not_applicable"
    if mode == "full_ai":
        assert result["ai_allocation"]["purchase_search"]["status"] == "bounded"
        assert result["ai_allocation"]["purchase_search"]["no_positive_feasible"] is None


@pytest.mark.parametrize("change", ["ledger", "currency", "freshness"])
def test_changes_during_executor_work_are_rechecked_before_return(environment, monkeypatch, change):
    approved_projection(monkeypatch, environment, {"SAP.DE": 100.}, {"SAP.DE": 1.})

    async def execute(job):
        calculated = await asyncio.to_thread(job)
        assert calculated[1]["deployed"] > 0
        if change == "ledger":
            environment.manager.store.async_user.return_value["holdings"] = [{
                **environment.candidate,
                "transactions": [{"id": "new-buy", "type": "buy", "date": "2026-10-06", "quantity": 1.}],
            }]
        elif change == "currency":
            environment.manager.store.async_user.return_value["base_currency"] = "USD"
        else:
            environment.clock.now += 10 * 86400
        return calculated

    environment.manager.hass.async_add_executor_job = AsyncMock(side_effect=execute)
    result = _indication(environment, amount=100., existing_cash=100., whole_units_only=True,
                         max_candidate_pct=100.)
    assert result["allocation"]["deployed"] == 0
    assert result["allocation"]["cash_reserve"] == 100
    assert result["results"][0]["allocation_eligible"] is False
    if change != "freshness":
        assert result["allocation"]["portfolio_risk"]["ledger_changed_during_analysis"] is True
