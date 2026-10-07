"""Independent adversarial checks of the final full-portfolio purchase gate."""
from copy import deepcopy
from datetime import date, timedelta
import importlib
import json
import math
from pathlib import Path
import random
import sys
from types import ModuleType

import pytest


COMP = Path(__file__).resolve().parents[1] / "custom_components/investment"
PACKAGE = "_portfolio_plan_adversarial_package"
package = ModuleType(PACKAGE)
package.__path__ = [str(COMP)]
sys.modules[PACKAGE] = package
subject = importlib.import_module(f"{PACKAGE}.portfolio_plan")

ZERO_COSTS = {"confirmed": True, "fixed_fee": 0., "commission_pct": 0., "spread_bps": 0., "fx_bps": 0.}
AS_OF = date(2026, 10, 6)


def position(name="A", *, quantity=0., price=10., principal=20., sleeve="broad_equity", values=None):
    if values is None:
        values = [(-.002 if index % 2 else .002) for index in range(156)]
    history = {(date(2023, 1, 2) + timedelta(weeks=index)).strftime("%G-W%V"): value
               for index, value in enumerate(values)}
    return {"provider": "test", "provider_id": name, "symbol": name,
            "category": "stock", "economic_sleeve": sleeve,
            "portfolio_price": price, "quantity": quantity,
            "suggested_amount": principal,
            "suggested_units": principal / price if price else 0.,
            "allocation_eligible": True, "risk_weekly_returns": history}


def run(rows, budget=100., *, holdings=(), cash=0., **kwargs):
    return subject.finalize_purchase_plan(
        rows, budget, kwargs.pop("risk", "medium"),
        existing_positions=holdings, existing_cash=cash,
        execution_costs=kwargs.pop("execution_costs", ZERO_COSTS),
        as_of=AS_OF, bootstrap_repetitions=0, **kwargs,
    )


def test_positive_control_retains_valid_purchase_and_counts_holdings_plus_cash_pool():
    held = position("OLD", quantity=2, sleeve="government_bond")
    rows, meta = run([position()], holdings=[held], cash=80)
    assert rows[0]["suggested_amount"] == pytest.approx(20)
    risk = meta["portfolio_risk"]
    assert risk["status"] == "within_limits"
    assert risk["pre"]["portfolio_value"] == 100
    assert risk["baseline_with_contribution"]["portfolio_value"] == 200
    assert risk["post"]["portfolio_value"] == 200
    assert risk["post"]["cash"] == 160
    assert risk["post"]["sleeve_weights"] == {"government_bond": .1, "broad_equity": .1}


@pytest.mark.parametrize("bad", [None, True, False, math.nan, math.inf, -math.inf, -1., "10"])
def test_invalid_nonzero_held_quantity_cannot_disappear_into_other_valid_holdings(bad):
    bad_holding = position("UNKNOWN", quantity=bad)
    rows, meta = run([position()], holdings=[position("KNOWN", quantity=1), bad_holding])
    assert rows[0]["suggested_amount"] == 0
    assert meta["portfolio_risk"]["status"] == "unknown"
    assert meta["portfolio_risk"]["existing_value"] is None
    assert "existing_position_value_unavailable" in meta["portfolio_risk"]["blockers"]
    json.dumps(meta, allow_nan=False)


@pytest.mark.parametrize("bad", [None, True, False, math.nan, math.inf, -math.inf, -1., 0., "10"])
def test_invalid_held_mark_price_blocks_instead_of_treating_unknown_position_as_cash(bad):
    held = position("UNKNOWN", quantity=1)
    held["portfolio_price"] = bad
    rows, meta = run([position()], holdings=[held])
    assert rows[0]["suggested_amount"] == 0
    assert meta["portfolio_risk"]["status"] == "unknown"
    assert meta["portfolio_risk"]["post"] is None


def test_positive_underflowing_held_value_cannot_disappear_or_claim_cash_only():
    held = position("UNDERFLOW", quantity=5e-324)
    held["portfolio_price"] = 5e-324
    held["risk_weekly_returns"] = {}
    rows, meta = run([position()], holdings=[held])
    assert rows[0]["suggested_amount"] == 0
    assert meta["portfolio_risk"]["status"] == "unknown"
    assert meta["portfolio_risk"]["existing_value"] is None


def test_overflowing_sum_of_finite_held_values_returns_unknown_without_exception():
    holdings = [position("BIG_A", quantity=1e308, price=1), position("BIG_B", quantity=1e308, price=1)]
    rows, meta = run([position()], holdings=holdings)
    assert rows[0]["suggested_amount"] == 0
    assert meta["portfolio_risk"]["status"] == "unknown"
    assert meta["portfolio_risk"]["existing_value"] is None
    json.dumps(meta, allow_nan=False)


def test_true_zero_quantity_needs_no_history_price_or_identity():
    held = {"quantity": 0., "portfolio_price": None, "data_quality_eligible": False}
    rows, meta = run([position()], holdings=[held])
    assert rows[0]["suggested_amount"] > 0
    assert meta["portfolio_risk"]["status"] == "within_limits"


def test_tiny_positive_missing_history_remains_blocking():
    held = position("MISSING", quantity=1e-16)
    held["risk_weekly_returns"] = {}
    rows, meta = run([position()], holdings=[held])
    assert rows[0]["suggested_amount"] == 0
    assert meta["portfolio_risk"]["status"] == "unknown"


@pytest.mark.parametrize("field", ["data_quality_eligible", "instrument_identity_eligible"])
def test_explicit_failed_held_evidence_cannot_borrow_purchase_candidate_verification(field):
    held = position("FAILED", quantity=1)
    held[field] = False
    rows, meta = run([position()], holdings=[held])
    assert rows[0]["suggested_amount"] == 0
    assert "existing_position_evidence_unavailable" in meta["portfolio_risk"]["blockers"]


def test_confirmed_fees_reduce_final_wealth_and_cash_exactly_once():
    held = position("OLD", quantity=2, sleeve="government_bond")
    costs = {**ZERO_COSTS, "fixed_fee": 1.0, "commission_pct": 1.0}
    rows, meta = run([position()], holdings=[held], cash=80, execution_costs=costs)
    assert rows[0]["suggested_amount"] == pytest.approx(20)
    assert meta["execution_costs"]["estimated_cash_debit"] == pytest.approx(21.2)
    assert meta["portfolio_risk"]["post"]["cash"] == pytest.approx(158.8)
    assert meta["portfolio_risk"]["post"]["portfolio_value"] == pytest.approx(198.8)
    assert meta["portfolio_risk"]["post"]["sleeve_weights"]["broad_equity"] == pytest.approx(20 / 198.8)


def test_missing_cost_confirmation_blocks_purchases_without_fabricating_free_execution():
    rows, meta = run([position()], execution_costs=None)
    assert rows[0]["suggested_amount"] == 0
    assert meta["execution_costs"]["status"] == "unknown"
    assert meta["execution_costs"]["estimated_cash_debit"] == 0
    assert "execution_costs_unknown" in meta["portfolio_risk"]["blockers"]


def test_instrument_concentration_aggregates_existing_and_new_same_provider_position():
    held = position("A", quantity=20)
    rows, meta = run([position("A", principal=50)], holdings=[held], cash=100, max_candidate_fraction=.5)
    assert rows[0]["suggested_amount"] <= 1e-8
    assert meta["portfolio_risk"]["baseline_with_contribution"]["instrument_weights"]["provider:test:A"] == .5
    assert meta["portfolio_risk"]["post"]["instrument_weights"]["provider:test:A"] <= .5 + 1e-12


def test_existing_breach_baseline_includes_entire_designated_cash_pool():
    held = position("A", quantity=80)
    # Existing concentration is80%, then unspent100cash makes the pretrade
    # reference72.727%. Spending any of that cash on the same sleeve worsens it.
    rows, meta = run([position("B", principal=20)], holdings=[held], cash=200)
    assert rows[0]["suggested_amount"] <= 1e-8
    assert meta["portfolio_risk"]["baseline_with_contribution"]["sleeve_weights"]["broad_equity"] == pytest.approx(800 / 1100)
    assert meta["portfolio_risk"]["status"] == "existing_breach_not_worsened"


def test_existing_breach_can_accept_other_sleeve_without_claiming_within_limits():
    held = position("A", quantity=80)
    rows, meta = run([position("B", principal=20, sleeve="government_bond")], holdings=[held], cash=200)
    assert rows[0]["suggested_amount"] > 0
    assert meta["portfolio_risk"]["status"] == "existing_breach_not_worsened"
    assert "sleeve:broad_equity" in meta["portfolio_risk"]["post_breaches"]


def test_cost_reducing_denominator_cannot_silently_worsen_existing_concentration():
    held = position("A", quantity=80)
    costs = {**ZERO_COSTS, "fixed_fee": 1.0}
    rows, meta = run([position("B", principal=20, sleeve="government_bond")], holdings=[held], cash=200, execution_costs=costs)
    assert rows[0]["suggested_amount"] == 0
    assert meta["portfolio_risk"]["status"] == "existing_breach_not_worsened"
    assert meta["execution_costs"]["estimated_transaction_cost"] == 0


def test_optional_drawdown_limit_closes_sustained_decline_at_final_purchase_gate():
    candidate = position(values=[-.01] * 52, principal=50)
    unbounded, _ = run([candidate], risk="medium")
    bounded, meta = run([candidate], risk="medium", max_drawdown_loss=.1)
    assert bounded[0]["suggested_amount"] < unbounded[0]["suggested_amount"]
    assert meta["portfolio_risk"]["post"]["max_drawdown_3y"] <= .1 + 1e-12
    assert meta["portfolio_risk"]["evidence"]["drawdown_limit"]["status"] == "pass"


def test_calendar_gaps_cannot_supply_drawdown_safety_for_existing_positions():
    held = position("GAPS", quantity=1)
    del held["risk_weekly_returns"][list(held["risk_weekly_returns"])[52]]
    rows, meta = run([position()], holdings=[held], cash=100, max_drawdown_loss=.2)
    assert rows[0]["suggested_amount"] == 0
    assert meta["portfolio_risk"]["status"] == "unknown"
    assert "portfolio_drawdown_calendar_gaps" in meta["portfolio_risk"]["blockers"]


def test_calendar_gaps_do_not_prevent_distribution_diagnostic_when_no_path_bound_exists():
    held = position("GAPS", quantity=1, sleeve="government_bond")
    del held["risk_weekly_returns"][list(held["risk_weekly_returns"])[52]]
    rows, meta = run([position()], holdings=[held], cash=100)
    assert rows[0]["suggested_amount"] > 0
    assert meta["portfolio_risk"]["post"]["max_drawdown_3y"] is None
    assert meta["portfolio_risk"]["evidence"]["windows"]["full"]["path"] is None


def test_ai_selective_hedge_removal_gets_rechecked_against_current_holdings():
    series = [-.08 if index % 2 else .08 for index in range(156)]
    held = position("HELD", quantity=10, sleeve="broad_equity", values=series)
    first = position("SAME", principal=40, sleeve="broad_equity", values=series)
    hedge = position("HEDGE", principal=40, sleeve="government_bond", values=[-value for value in series])
    deterministic, meta = run([first, hedge], holdings=[held], cash=200, risk="low")
    before_risk = meta["portfolio_risk"]["post"]["annualized_volatility_3y"]
    for row in deterministic:
        row["ai_suggested_amount"] = row["suggested_amount"] if row["symbol"] == "SAME" else 0.
        row["ai_suggested_units"] = row["suggested_units"] if row["symbol"] == "SAME" else 0.
        row["ai_action"] = "consider"
    ai_rows, ai_meta = run(deterministic, holdings=[held], cash=200, risk="low", amount_key="ai_suggested_amount", units_key="ai_suggested_units")
    assert ai_rows[1]["ai_suggested_amount"] == 0
    assert ai_rows[0]["ai_suggested_amount"] < deterministic[0]["suggested_amount"]
    assert ai_meta["portfolio_risk"]["post"]["annualized_volatility_3y"] <= max(ai_meta["portfolio_risk"]["limits"]["annualized_volatility_3y"], ai_meta["portfolio_risk"]["baseline_with_contribution"]["annualized_volatility_3y"]) + 1e-12
    assert before_risk <= ai_meta["portfolio_risk"]["post"]["annualized_volatility_3y"] + 1e-12


def test_expired_evidence_after_ai_cannot_be_revived_by_ai_positive_numbers():
    row = position()
    row.update(data_quality_eligible=False, ai_suggested_amount=20., ai_suggested_units=2., ai_action="consider")
    rows, meta = run([row], amount_key="ai_suggested_amount", units_key="ai_suggested_units")
    assert rows[0]["ai_suggested_amount"] == 0
    assert rows[0]["ai_action"] == "watch"


def test_analysis_only_amount_none_preserves_absent_suggestions_and_charges_nothing():
    row = position()
    row["suggested_amount"] = row["suggested_units"] = None
    rows, meta = run([row], budget=None, holdings=[position("HELD", quantity=1)], cash=100)
    assert rows[0]["suggested_amount"] is None
    assert rows[0]["suggested_units"] is None
    assert meta["deployed"] is None
    assert meta["execution_costs"]["status"] == "not_requested"
    assert meta["portfolio_risk"]["status"] == "analysis_only"
    assert meta["portfolio_risk"]["post"]["portfolio_value"] == 110


def test_ignore_context_excludes_holdings_only_when_explicitly_requested():
    unknown = position(quantity=None)
    rows, meta = run([position()], holdings=[unknown], portfolio_context="ignore")
    assert rows[0]["suggested_amount"] > 0
    assert meta["portfolio_risk"]["scope"] == "new_contribution_only"


@pytest.mark.parametrize("seed", range(15))
def test_randomized_plans_preserve_accounting_limits_and_input_ceilings(seed):
    rng = random.Random(seed)
    budget = round(rng.uniform(40, 1000), 2)
    cash = round(rng.uniform(100, 1000), 2)
    held = position("HELD", quantity=rng.uniform(.1, 10), sleeve="government_bond")
    rows = [position("A", principal=budget * .2), position("B", principal=budget * .15, sleeve="government_bond")]
    original = deepcopy(rows)
    profile = {**ZERO_COSTS, "fixed_fee": rng.uniform(0, 2), "commission_pct": rng.uniform(0, .5), "spread_bps": rng.uniform(0, 20)}
    result, meta = run(rows, budget, holdings=[held], cash=cash, execution_costs=profile)
    costs = meta["execution_costs"]
    assert costs["estimated_cash_debit"] <= budget + 1e-12
    assert costs["cash_remaining"] + costs["estimated_cash_debit"] == pytest.approx(budget)
    assert costs["principal"] + costs["estimated_transaction_cost"] == pytest.approx(costs["estimated_cash_debit"])
    assert meta["portfolio_risk"]["post"]["portfolio_value"] == pytest.approx(held["quantity"] * held["portfolio_price"] + cash + budget - costs["estimated_transaction_cost"])
    for before, after in zip(rows, result):
        assert 0 <= after["suggested_amount"] <= before["suggested_amount"] + 1e-10
        assert 0 <= after["suggested_units"] <= before["suggested_units"] + 1e-10
        assert after["suggested_amount"] == pytest.approx(after["suggested_units"] * after["portfolio_price"])
    assert rows == original
    assert subject._non_worsening(meta["portfolio_risk"]["post"], meta["portfolio_risk"]["baseline_with_contribution"], meta["portfolio_risk"]["limits"])
    json.dumps(meta, allow_nan=False)
