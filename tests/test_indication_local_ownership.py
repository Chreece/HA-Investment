"""Contribution analysis needs local ownership, not unrelated market valuations."""
import asyncio
from copy import deepcopy
import importlib

import pytest

from test_indication_evidence_integration import (
    _indication,
    _portfolio_with,
    environment,
    modules,
)


def contribution_manager(environment):
    contribution = importlib.import_module(
        environment.modules.runtime.__package__ + ".contribution_manager"
    )
    environment.manager.__class__ = contribution.InvestmentManager
    return environment.manager


def refuse_display_enrichment(environment):
    display = environment.modules.core.InvestmentManager.async_portfolio
    display.side_effect = AssertionError("Ownership must not fetch portfolio quotes or FX")
    return display


@pytest.mark.parametrize("scope", ["search", "portfolio"])
def test_contribution_sources_do_not_request_display_market_valuation(environment, scope):
    manager = contribution_manager(environment)
    held = {
        **environment.candidate, "id": "owned", "quantity": 2., "value": 1000000.,
    }
    if scope == "search":
        held.update(provider="unavailable-feed", provider_id="UNRELATED", symbol="UNRELATED")
    _portfolio_with(environment, [held])
    display = refuse_display_enrichment(environment)

    result = _indication(environment, scope=scope, portfolio_context="use")

    display.assert_not_awaited()
    assert result["scope"] == scope
    assert result["risk_scope"] == "new_contribution_only"
    assert result["complete_portfolio_enforcement"] is False
    assert result["allocation"]["deployed"] > 0
    assert all(call.args[0] == "SAP.DE" for call in manager.yahoo._chart.await_args_list)
    assert result["allocation"]["portfolio_risk"]["existing_value"] == 0


def test_owned_filter_uses_fresh_ledger_when_display_cache_reports_zero(environment):
    manager = contribution_manager(environment)
    _portfolio_with(environment, [{
        **environment.candidate, "id": "owned", "quantity": 0., "value": 0.,
        "transactions": [{"id": "buy", "type": "buy", "date": "2026-01-02", "quantity": 2.}],
    }])
    manager._cache.set(("portfolio", "fixture-user"), {
        "holdings": [{**environment.candidate, "quantity": 0., "status": "error"}],
        "categories": [], "total": 0.,
    })
    display = refuse_display_enrichment(environment)

    with pytest.raises(ValueError, match="No new instruments remain"):
        _indication(environment, portfolio_context="use")

    display.assert_not_awaited()
    manager.yahoo._chart.assert_not_awaited()


def test_completed_sale_does_not_exclude_candidate_using_old_aggregate_quantity(environment):
    contribution_manager(environment)
    _portfolio_with(environment, [{
        **environment.candidate, "id": "sold", "quantity": 3., "value": 999.,
        "transactions": [
            {"id": "buy", "type": "buy", "date": "2026-01-02", "quantity": 3.},
            {"id": "sell", "type": "sell", "date": "2026-02-02", "quantity": 3.},
        ],
    }])
    display = refuse_display_enrichment(environment)

    result = _indication(environment, portfolio_context="use")

    display.assert_not_awaited()
    assert result["results"][0]["already_owned"] is False
    assert result["allocation"]["deployed"] > 0


def test_fresh_local_snapshot_does_not_overwrite_display_cache(environment):
    manager = contribution_manager(environment)
    _portfolio_with(environment, [{
        **environment.candidate, "id": "owned", "quantity": 2., "value": 200.,
    }])
    cached = {"holdings": [], "total": 4321., "bootstrap_local": False}
    manager._cache.set(("portfolio", "fixture-user"), deepcopy(cached))

    local = asyncio.run(manager.async_portfolio_bootstrap("fixture-user", use_cache=False))

    assert local["bootstrap_local"] is True
    assert local["holdings"][0]["quantity"] == 2.
    assert local["holdings"][0]["price"] is None
    assert manager._cache.get(("portfolio", "fixture-user"), 60) == cached
    manager.yahoo._chart.assert_not_awaited()


def test_normal_dashboard_still_uses_market_valuation(environment):
    manager = contribution_manager(environment)
    display = environment.modules.core.InvestmentManager.async_portfolio
    payload = {"holdings": [], "total": 123.}
    display.return_value = payload

    result = asyncio.run(manager.async_portfolio("fixture-user", force=True, refresh_market=True))

    assert result is payload
    display.assert_awaited_once_with("fixture-user", force=True, refresh_market=True)
