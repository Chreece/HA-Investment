"""Focused controls for bounded whole-unit recovery and its declared scope."""
from copy import deepcopy
from datetime import date, timedelta
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest


PACKAGE = "_purchase_search_test_package"
package = ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components/investment")]
sys.modules[PACKAGE] = package
plan = importlib.import_module(f"{PACKAGE}.portfolio_plan")
search = importlib.import_module(f"{PACKAGE}.purchase_search")
costs = importlib.import_module(f"{PACKAGE}.execution_costs")
AS_OF = date(2026, 10, 6)
COSTS = {"confirmed": True, "fixed_fee": 1., "commission_pct": 0., "spread_bps": 0., "fx_bps": 0.}


def row(name, price, sleeve="government_bond", *, units=1., whole=True, **changes):
    history = {(date(2023, 1, 2) + timedelta(weeks=i)).strftime("%G-W%V"):
               (.002 if i % 2 else -.002) for i in range(156)}
    return {"provider": "test", "provider_id": name, "symbol": name,
            "portfolio_price": price, "suggested_amount": price * units,
            "suggested_units": units, "whole_units_only": whole,
            "allocation_eligible": True, "economic_sleeve": sleeve,
            "risk_weekly_returns": history, **changes}


def run(rows, budget=100., **changes):
    return plan.finalize_purchase_plan(
        rows, budget, "medium", execution_costs=changes.pop("execution_costs", COSTS),
        as_of=AS_OF, bootstrap_repetitions=0, **changes,
    )


def counterexample():
    return [row("A", 40., "cash_like"), row("B", 30., "government_bond"),
            row("C", 30., "aggregate_bond")]


def test_whole_fee_cliff_recovers_original_positive_subset_with_exact_costs():
    original = counterexample()
    frozen = deepcopy(original)
    proportional, before = costs.apply_execution_costs(original, 100., COSTS)
    assert [item["suggested_units"] for item in proportional] == [0., 0., 0.]
    assert before["cash_remaining"] == 100.
    result, meta = run(original, max_candidate_fraction=.45)
    assert [item["suggested_units"] for item in result] == [1., 1., 0.]
    assert meta["deployed"] == 70.
    assert meta["execution_costs"]["estimated_cash_debit"] == 72.
    assert meta["execution_costs"]["estimated_transaction_cost"] == 2.
    assert meta["cash_reserve"] == 28.
    details = meta["purchase_search"]
    assert details["status"] == "exhaustive"
    assert details["total_combinations"] == details["generated"] == 8
    assert details["examined"] == 1
    assert details["optimality_proven"] is True
    assert details["global_optimality_claimed"] is False
    assert details["selected_source"] == "whole_unit_search"
    assert meta["portfolio_risk"]["risk_scale"] is None
    assert meta["portfolio_risk"]["status"] == "within_limits"
    assert original == frozen
    json.dumps(meta, allow_nan=False)


@pytest.mark.parametrize("rejection", [
    {"suggested_amount": 0.}, {"suggested_units": 0.}, {"allocation_eligible": False},
    {"data_quality_eligible": False}, {"instrument_identity_eligible": False},
])
def test_search_cannot_revive_original_zero_or_ineligible_rows(rejection):
    original = counterexample() + [row("FORBIDDEN", 27., "broad_equity", **rejection)]
    result, meta = run(original, max_candidate_fraction=.45)
    assert result[-1]["suggested_amount"] == result[-1]["suggested_units"] == 0.
    assert meta["purchase_search"]["whole_candidates"] == 3
    assert meta["deployed"] == 70.


def test_fractional_incumbent_is_fixed_when_whole_subset_is_recovered():
    original = [row("A", 40., "cash_like"), row("B", 40., "government_bond"),
                row("F", 10., "aggregate_bond", units=2., whole=False)]
    incumbent, _ = costs.apply_execution_costs(original, 100., COSTS)
    result, meta = run(original, max_candidate_fraction=.45)
    assert sum(item["suggested_units"] for item in result[:2]) == 1.
    assert result[2]["suggested_units"] == incumbent[2]["suggested_units"]
    assert result[2]["suggested_amount"] == incumbent[2]["suggested_amount"]
    assert meta["purchase_search"]["fractional_incumbent_fixed"] is True
    assert meta["purchase_search"]["fractional_candidates"] == 1
    assert meta["execution_costs"]["estimated_cash_debit"] <= 100.


def test_stable_identity_tiebreak_survives_permutation_of_distinct_rows():
    original = counterexample()
    a, _ = run(original, max_candidate_fraction=.45)
    b, _ = run(list(reversed(original)), max_candidate_fraction=.45)
    assert {item["provider_id"]: item["suggested_units"] for item in a} == {
        item["provider_id"]: item["suggested_units"] for item in b
    }


def test_accepted_original_maximum_proves_upper_bound_without_search_snapshots():
    result, meta = run([row("A", 20.)], max_candidate_fraction=.45)
    assert result[0]["suggested_units"] == 1.
    details = meta["purchase_search"]
    assert details["status"] == "upper_bound_verified"
    assert details["examined"] == details["generated"] == 0
    assert details["optimality_proven"] is True
    assert details["no_positive_feasible"] is False


def test_small_full_domain_can_prove_no_positive_plan_under_unchanged_limits():
    result, meta = run([row("A", 60.), row("B", 60., "aggregate_bond")],
                       max_candidate_fraction=.45)
    assert all(item["suggested_units"] == 0. for item in result)
    details = meta["purchase_search"]
    assert details["full_domain_enumerated"] is True
    assert details["optimality_proven"] is True
    assert details["no_positive_feasible"] is True
    assert details["risk_rejected"] == 2
    assert "execution_costs_no_affordable_purchase" not in meta["execution_costs"]["reasons"]
    assert "portfolio_risk_no_verified_purchase" in meta["portfolio_risk"]["blockers"]
    assert "portfolio_limit:instrument:provider:test:A" in details["risk_rejection_issues"]
    assert all("execution_costs_no_affordable_purchase" not in item["execution_cost_blockers"]
               for item in result)
    assert meta["portfolio_risk"]["status"] == "within_limits"


def test_work_limit_stops_search_without_claiming_no_positive_plan(monkeypatch):
    monkeypatch.setattr(search, "MAX_SEARCH_RETURN_POINTS", 1)
    original = [row("A", 60.), row("B", 20., "aggregate_bond")]
    # A prevents every positive proportional whole result. A is also the first
    # higher-deployment discrete choice but breaches the 45% instrument limit;
    # B would be valid, beyond the single additional snapshot budget.
    result, meta = run(original, max_candidate_fraction=.45)
    assert all(item["suggested_units"] == 0. for item in result)
    details = meta["purchase_search"]
    assert details["risk_evaluation_limit"] == details["examined"] == 1
    assert details["stopped_by_work_limit"] is True
    assert details["status"] == "bounded"
    assert details["optimality_proven"] is False
    assert details["no_positive_feasible"] is None
    assert details["risk_rejected"] == 1
    assert "portfolio_risk_no_verified_purchase" in meta["portfolio_risk"]["blockers"]
    assert "execution_costs_no_affordable_purchase" not in meta["execution_costs"]["reasons"]


def test_ai_amount_and_quantity_ceilings_bound_recovery_and_keep_rejected_hedge_zero():
    original = counterexample()
    for item in original:
        item.update(ai_suggested_amount=item["suggested_amount"],
                    ai_suggested_units=item["suggested_units"], ai_action="consider")
    original[-1].update(ai_suggested_amount=0., ai_suggested_units=0., ai_action="avoid")
    result, meta = run(original, amount_key="ai_suggested_amount", units_key="ai_suggested_units")
    assert result[-1]["ai_suggested_amount"] == result[-1]["ai_suggested_units"] == 0.
    assert result[-1]["ai_action"] == "avoid"
    assert meta["purchase_search"]["whole_candidates"] == 2
    for before, after in zip(original, result, strict=True):
        assert after["ai_suggested_amount"] <= before["ai_suggested_amount"]
        assert after["ai_suggested_units"] <= before["ai_suggested_units"]
        assert after["suggested_amount"] == before["suggested_amount"]


@pytest.mark.parametrize("bad_history", [None, True, False, 1, "invalid", [], {}])
def test_work_budget_handles_rejected_malformed_history_without_exception(bad_history):
    assert search.risk_evaluation_limit([{"risk_weekly_returns": bad_history}], []) > 0


def test_unknown_costs_block_search_without_claiming_domain_impossibility():
    _, meta = run(counterexample(), execution_costs=None)
    assert meta["purchase_search"]["status"] == "blocked"
    assert meta["purchase_search"]["no_positive_feasible"] is None
    assert meta["purchase_search"]["optimality_proven"] is False


def test_cash_infeasibility_without_risk_rejection_retains_affordability_explanation():
    _, meta = run([row("A", 20.)], execution_costs={**COSTS, "fixed_fee": 101.})
    assert meta["purchase_search"]["risk_rejected"] == 0
    assert meta["purchase_search"]["no_positive_feasible"] is True
    assert "execution_costs_no_affordable_purchase" in meta["execution_costs"]["reasons"]
    assert "portfolio_risk_no_verified_purchase" not in meta["portfolio_risk"]["blockers"]
