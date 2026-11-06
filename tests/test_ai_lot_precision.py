"""AI whole-lot principal survives repeated gates without enlarging authority."""
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
import math
from pathlib import Path
import sys

import pytest


COMP = Path(__file__).resolve().parents[1] / "custom_components" / "investment"


def load(name, filename):
    spec = spec_from_file_location(name, COMP / filename)
    module = module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


model = load("investment_ai_lot_precision_model", "validated_model.py")
costs = load("investment_ai_lot_precision_costs", "execution_costs.py")
ZERO_COSTS = {
    "confirmed": True, "fixed_fee": 0., "commission_pct": 0.,
    "spread_bps": 0., "fx_bps": 0.,
}


def clamp(price, deterministic, requested, *, budget=100., whole=True, categories=None, **changes):
    rows = [{
        "provider": "yahoo", "provider_id": "RAW", "symbol": "RAW",
        "category": "stock", "portfolio_price": price,
        "suggested_amount": deterministic,
        "suggested_units": deterministic / price,
        "allocation_eligible": True,
        "risk_weekly_returns": {f"w{i}": 0. for i in range(60)},
        **changes,
    }]
    ranking = [{
        "provider": "yahoo", "provider_id": "RAW", "action": "consider",
        "score": 75, "suggested_amount": requested,
    }]
    meta = model.clamp_ai_ranking_to_deterministic(
        rows, ranking, budget, risk="medium",
        whole_units_only=whole, whole_unit_categories=categories,
    )
    return rows, meta


@pytest.mark.parametrize("price,deterministic,requested,units", [
    (4.914, 4.914, 4.914, 1),
    (4.916, 4.916, 4.916, 1),
    (4.914, 14.742, 14.742, 3),
    (4.911, 9.822, 9.822, 2),
    (0.3333, 0.9999, 0.9999, 3),
    (4.914, 9.828, 4.914, 1),
    (4.914, 4.914, 9.828, 1),
    (0.10000000000000002, 0.3000000000000001, 0.3000000000000001, 3),
])
def test_raw_principal_and_exact_whole_units_survive_both_ai_gates(price, deterministic, requested, units):
    rows, meta = clamp(price, deterministic, requested)
    row = rows[0]
    principal = Decimal(str(price)) * units
    assert row["ai_suggested_units"] == units
    assert row["ai_action"] == "consider"
    assert principal <= Decimal(str(row["ai_suggested_amount"])) <= Decimal(str(min(deterministic, requested)))
    assert row["ai_suggested_amount"] <= deterministic
    assert row["ai_suggested_amount"] <= requested
    assert meta["post_ai_risk"]["risk_scale"] == 1
    assert meta["post_ai_risk"]["adjusted"] is False
    # A later unchanged cash gate must retain those units. Cash accounting may
    # round debit up, but must never be reused as extra purchase authority.
    purchased, summary = costs.apply_execution_costs(
        rows, 100., ZERO_COSTS, amount_key="ai_suggested_amount", units_key="ai_suggested_units",
    )
    assert purchased[0]["ai_suggested_units"] == units
    assert purchased[0]["ai_suggested_amount"] <= min(deterministic, requested)
    assert summary["purchase_count"] == 1


@pytest.mark.parametrize("deterministic,requested", [
    (4.914, math.nextafter(4.914, 0.)),
    (math.nextafter(4.914, 0.), 4.914),
    (4.914, 4.91),
    (4.91, 4.914),
    (0., 4.914),
    (4.914, 0.),
])
def test_even_a_one_float_step_shortfall_cannot_borrow_purchase_authority(deterministic, requested):
    rows, _ = clamp(4.914, deterministic, requested)
    assert rows[0]["ai_suggested_units"] == 0
    assert rows[0]["ai_suggested_amount"] == 0
    assert rows[0]["ai_action"] == "watch"


def test_category_based_whole_unit_rule_uses_the_same_raw_principal_contract():
    rows, _ = clamp(4.914, 4.914, 4.914, whole=False, categories=["stock"])
    assert rows[0]["ai_suggested_units"] == 1
    assert rows[0]["ai_suggested_amount"] == 4.914


@pytest.mark.parametrize("budget,reserve,fee,units,debit", [
    (4.92, 0., 0., 1, 4.92),
    (4.91, 0., 0., 0, 0.),
    (4.92, 0., .01, 0, 0.),
    (4.93, .01, 0., 1, 4.92),
    (4.92, .01, 0., 0, 0.),
    (4.93, 0., .01, 1, 4.93),
])
def test_raw_principal_retention_does_not_bypass_fees_or_reserved_cents(budget, reserve, fee, units, debit):
    rows, _ = clamp(4.914, 4.914, 4.914)
    purchased, summary = costs.apply_execution_costs(
        rows, budget, {**ZERO_COSTS, "fixed_fee": fee},
        amount_key="ai_suggested_amount", units_key="ai_suggested_units",
        reserve_amount=reserve,
    )
    assert purchased[0]["ai_suggested_units"] == units
    assert purchased[0]["ai_estimated_cash_debit"] == debit
    assert summary["cash_remaining"] >= reserve
    assert purchased[0]["ai_suggested_amount"] <= 4.914


@pytest.mark.parametrize("flag", ["allocation_eligible", "data_quality_eligible", "instrument_identity_eligible"])
def test_failed_eligibility_remains_closed_despite_a_complete_raw_price_request(flag):
    rows, _ = clamp(4.914, 4.914, 4.914, **{flag: False})
    assert rows[0]["ai_suggested_units"] == 0
    assert rows[0]["ai_suggested_amount"] == 0


def test_missing_execution_costs_still_block_raw_principal_after_ai_validation():
    rows, _ = clamp(4.914, 4.914, 4.914)
    purchased, summary = costs.apply_execution_costs(
        rows, 100., None, amount_key="ai_suggested_amount", units_key="ai_suggested_units",
    )
    assert purchased[0]["ai_suggested_units"] == 0
    assert purchased[0]["ai_suggested_amount"] == 0
    assert summary["status"] == "unknown"


def test_ai_removing_a_hedge_still_triggers_whole_unit_risk_reduction():
    returns = {f"w{i}": (.2 if i % 2 else -.2) for i in range(60)}
    rows = [{
        "provider": "fixture", "provider_id": identifier, "symbol": identifier,
        "portfolio_price": 4.914, "suggested_amount": 4.914,
        "suggested_units": 1., "allocation_eligible": True,
        "risk_weekly_returns": {week: value * sign for week, value in returns.items()},
    } for identifier, sign in (("FIRST", 1), ("HEDGE", -1))]
    meta = model.clamp_ai_ranking_to_deterministic(
        rows, [
            {"provider": "fixture", "provider_id": "FIRST", "action": "consider", "suggested_amount": 4.914},
            {"provider": "fixture", "provider_id": "HEDGE", "action": "avoid", "suggested_amount": 0.},
        ], 10., risk="medium", whole_units_only=True,
    )
    assert meta["post_ai_risk"]["adjusted"] is True
    assert all(row["ai_suggested_amount"] == 0 for row in rows)
    assert all(row["ai_suggested_units"] == 0 for row in rows)
