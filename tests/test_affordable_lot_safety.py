"""Exercise minimum-lot recovery through the cash gate and shipped manager."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from test_indication_evidence_integration import environment, modules, _indication


COMP = Path(__file__).resolve().parents[1] / "custom_components/investment"
PACKAGE = "_affordable_lot_safety_package"
package = ModuleType(PACKAGE)
package.__path__ = [str(COMP)]
sys.modules[PACKAGE] = package
lots = importlib.import_module(f"{PACKAGE}.affordable_lots")
ZERO_COSTS = {
    "confirmed": True, "fixed_fee": 0., "commission_pct": 0.,
    "spread_bps": 0., "fx_bps": 0.,
}


def result_for(price, *, costs=None):
    history = {
        (date(2023, 1, 2) + timedelta(weeks=index)).strftime("%G-W%V"):
        .001 * (-1 if index % 2 else 1)
        for index in range(156)
    }
    row = {
        "provider": "fixture", "provider_id": "BOND", "symbol": "BOND",
        "category": "etf", "economic_sleeve": "government_bond",
        "portfolio_price": price, "suggested_amount": 0., "suggested_units": 0.,
        "allocation_weight_after_constraints": .01, "allocation_eligible": False,
        "whole_units_only": True, "market_score": 60., "confidence": 1.,
        "metrics": {"suitability_eligible": True}, "risk_weekly_returns": history,
    }
    return {
        "mode": "deterministic", "results": [row],
        "preferences": {
            "whole_unit_categories": ["etf"],
            "execution_costs": deepcopy(ZERO_COSTS if costs is None else costs),
        },
        "allocation": {
            "max_candidate_fraction": .45,
            "portfolio_risk": {
                "scope": "new_contribution_only", "status": "within_limits",
                "portfolio_snapshot_issue": None, "ledger_changed_during_analysis": False,
            },
        },
    }


def recover(result, budget=100.):
    return lots.recover_affordable_minimum_lots(result, budget=budget, risk_tolerance="medium")


@pytest.mark.parametrize("price", [4.91, 4.914, 4.916, 4.906923076923077, 23.116666666666667])
def test_raw_price_minimum_lot_survives_the_real_final_cash_gate(price):
    result = recover(result_for(price))
    row = result["results"][0]
    costs = result["allocation"]["execution_costs"]
    assert row["suggested_units"] == 1.
    assert row["suggested_amount"] == price
    expected_debit = Decimal(str(price)).quantize(Decimal(".01"), rounding=ROUND_CEILING)
    assert Decimal(str(costs["estimated_cash_debit"])) == expected_debit
    assert Decimal(str(costs["cash_remaining"])) == Decimal(100) - expected_debit
    assert result["minimum_lot_override"]["used"] == 1
    assert result["allocation"]["portfolio_risk"]["scope"] == "new_contribution_only"
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("fee,commission,spread,fx", [(1., 0., 0., 0.), (.07, .25, 12., 10.)])
def test_raw_price_recovery_still_reserves_exact_cash_for_confirmed_costs(fee, commission, spread, fx):
    price = 4.914
    costs = {"confirmed": True, "fixed_fee": fee, "commission_pct": commission,
             "spread_bps": spread, "fx_bps": fx}
    result = recover(result_for(price, costs=costs))
    assert result["results"][0]["suggested_units"] == 1.
    rate = Decimal(str(commission)) / 100 + (Decimal(str(spread)) + Decimal(str(fx))) / 10000
    expected = (Decimal(str(price)) * (1 + rate) + Decimal(str(fee))).quantize(
        Decimal(".01"), rounding=ROUND_CEILING,
    )
    summary = result["allocation"]["execution_costs"]
    assert Decimal(str(summary["estimated_cash_debit"])) == expected
    assert Decimal(str(summary["cash_remaining"])) == Decimal(100) - expected


@pytest.mark.parametrize("fee,expected_units", [(0., 1.), (.01, 0.)])
def test_one_cent_fee_cannot_spend_the_requested_cash_reserve(fee, expected_units):
    result = result_for(4.914, costs={**ZERO_COSTS, "fixed_fee": fee})
    result["preferences"]["min_cash_reserve_pct"] = 95.08
    recovered = recover(result)
    assert recovered["results"][0]["suggested_units"] == expected_units
    if expected_units:
        assert recovered["allocation"]["cash_reserve"] == 95.08
        assert recovered["allocation"]["execution_costs"]["estimated_cash_debit"] == 4.92
    else:
        assert not recovered.get("minimum_lot_override")


@pytest.mark.parametrize("blocker", ["zero_weight", "data_quality", "suitability", "missing_risk", "candidate_cap", "reserve", "costs"])
def test_recovery_does_not_override_failed_evidence_or_cash_constraints(blocker):
    result = result_for(4.914)
    row = result["results"][0]
    if blocker == "zero_weight":
        row["allocation_weight_after_constraints"] = 0.
        row["allocation_weight_exact_validated"] = .4
    elif blocker == "data_quality":
        row["data_quality_eligible"] = False
    elif blocker == "suitability":
        row["metrics"]["suitability_eligible"] = False
    elif blocker == "missing_risk":
        row["risk_weekly_returns"] = {}
    elif blocker == "candidate_cap":
        result["allocation"]["max_candidate_fraction"] = .04
    elif blocker == "reserve":
        result["preferences"]["min_cash_reserve_pct"] = 96.
    elif blocker == "costs":
        result["preferences"]["execution_costs"]["confirmed"] = False
    original = deepcopy(result)
    assert recover(result) == original


@pytest.mark.parametrize("field,value", [
    ("portfolio_snapshot_issue", "portfolio_currency_changed_during_analysis"),
    ("portfolio_snapshot_issue", "portfolio_ledger_snapshot_unavailable"),
    ("ledger_changed_during_analysis", True),
    ("status", "unknown"), ("status", "blocked"),
    ("status", None), ("scope", "complete_portfolio"),
])
def test_recovery_preserves_global_final_validation_vetoes(field, value):
    result = result_for(4.91)
    result["allocation"]["portfolio_risk"][field] = value
    original = deepcopy(result)
    assert recover(result) == original


def test_missing_final_validation_cannot_authorize_minimum_lots():
    result = result_for(4.914)
    del result["allocation"]["portfolio_risk"]
    original = deepcopy(result)
    assert recover(result) == original


def test_recovery_cannot_enlarge_full_ai_purchase_authority():
    result = result_for(4.914)
    result["mode"] = "full_ai"
    result["results"][0].update(ai_suggested_amount=0., ai_suggested_units=0.)
    original = deepcopy(result)
    assert recover(result) == original


def _contribution_environment(environment, monkeypatch, price=4.91):
    contribution = importlib.import_module(f"{environment.modules.core.__package__}.contribution_manager")
    manager = object.__new__(contribution.InvestmentManager)
    manager.__dict__.update(environment.manager.__dict__)
    environment.manager = manager
    scale = price / environment.quote["meta"]["regularMarketPrice"]
    for payload in [environment.quote, *environment.histories.values()]:
        for category, field in (("quote", "close"), ("adjclose", "adjclose")):
            for series in payload["indicators"].get(category, []):
                series[field] = [value * scale for value in series[field]]
    environment.quote["meta"]["regularMarketPrice"] = price

    def zero_lot_projection(rows, weighted, risk, budget, **kwargs):
        # The initial monetary projection is the explicit fixture boundary.
        # The real score, constrained target, provider evidence, account checks,
        # final cost/risk gate and contribution recovery remain in production.
        output = deepcopy(rows)
        for row in output:
            row.update(suggested_amount=0., suggested_units=0.,
                       whole_units_only=True, allocation_eligible=False)
        return output, {"deployed": 0., "cash_reserve": budget, "deployment_fraction": 0.}

    monkeypatch.setattr(environment.modules.core, "production_projection", zero_lot_projection)
    return environment


def test_shipped_contribution_manager_keeps_a_raw_price_minimum_lot(environment, monkeypatch):
    environment = _contribution_environment(environment, monkeypatch, price=4.914)
    result = _indication(environment, amount=100., whole_units_only=True, max_candidate_pct=100.)
    assert result["results"][0]["allocation_weight_after_constraints"] > 0.
    assert result["results"][0]["suggested_units"] == 1.
    assert result["allocation"]["cash_reserve"] == 95.08
    assert result["risk_scope"] == "new_contribution_only"


def test_recovered_plan_does_not_reuse_ai_review_of_previous_purchases(environment, monkeypatch):
    environment = _contribution_environment(environment, monkeypatch)
    environment.manager._ai_indication_review = AsyncMock(return_value={
        "structured": {"verdict": "approve", "summary": "Review of the empty pre-recovery plan"},
    })
    result = _indication(environment, amount=100., whole_units_only=True,
                         max_candidate_pct=100., mode="deterministic_ai")
    assert result["results"][0]["suggested_units"] == 1.
    assert result["ai_review"] is None
    assert result["minimum_lot_override"]["ai_review_invalidated"] is True


@pytest.mark.parametrize("failure", ["currency", "snapshot_unavailable", "stale_source"])
def test_shipped_contribution_manager_does_not_resurrect_lots_after_final_veto(environment, monkeypatch, failure):
    environment = _contribution_environment(environment, monkeypatch)
    user = environment.manager.store.async_user.return_value
    fail_next_read = False

    async def read_user(*args, **kwargs):
        nonlocal fail_next_read
        if fail_next_read:
            fail_next_read = False
            raise RuntimeError("Synthetic final account snapshot failure")
        return user

    environment.manager.store.async_user.side_effect = read_user

    async def late_change(*args, **kwargs):
        nonlocal fail_next_read
        if failure == "currency":
            user["base_currency"] = "USD"
        elif failure == "snapshot_unavailable":
            fail_next_read = True
        else:
            environment.clock.now += 10 * 86400
        return {"structured": {"verdict": "approve", "summary": "Prior snapshot"}}

    environment.manager._ai_indication_review = AsyncMock(side_effect=late_change)
    result = _indication(environment, amount=100., whole_units_only=True,
                         max_candidate_pct=100., mode="deterministic_ai")
    assert result["results"][0]["allocation_weight_after_constraints"] > 0.
    assert result["results"][0]["suggested_units"] == 0.
    assert result["allocation"]["deployed"] == 0.
    assert result["allocation"]["cash_reserve"] == 100.
    assert not result.get("minimum_lot_override")
    assert result["ai_review"] is None
    if failure != "stale_source":
        risk = result["allocation"]["portfolio_risk"]
        assert risk["status"] == "unknown"
        assert risk["portfolio_snapshot_issue"] == (
            "portfolio_currency_changed_during_analysis" if failure == "currency"
            else "portfolio_ledger_snapshot_unavailable"
        )
