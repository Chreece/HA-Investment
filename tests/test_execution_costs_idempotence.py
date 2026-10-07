"""A returned purchase survives repricing without manufacturing new authority."""
from copy import deepcopy
from decimal import Decimal, ROUND_CEILING
from importlib.util import module_from_spec, spec_from_file_location
import json
import math
from pathlib import Path
import random

import pytest


SPEC = spec_from_file_location(
    "execution_costs_idempotence_subject",
    Path(__file__).resolve().parents[1] / "custom_components/investment/execution_costs.py",
)
costs = module_from_spec(SPEC)
SPEC.loader.exec_module(costs)

ZERO_COSTS = {"confirmed": True, "fixed_fee": 0., "commission_pct": 0.,
              "spread_bps": 0., "fx_bps": 0.}
RAW_PRICE = 23.252591845784377


def purchase(*, price=RAW_PRICE, amount=100., units=4., whole=True, ai=False):
    prefix = "ai_" if ai else ""
    return {"provider": "fixture", "provider_id": "precision",
            "portfolio_price": price, "whole_units_only": whole,
            "allocation_eligible": True,
            f"{prefix}suggested_amount": amount,
            f"{prefix}suggested_units": units}


def reprice(rows, budget, profile, *, ai=False, reserve=0.):
    prefix = "ai_" if ai else ""
    return costs.apply_execution_costs(
        rows, budget, profile, amount_key=f"{prefix}suggested_amount",
        units_key=f"{prefix}suggested_units", reserve_amount=reserve,
    )


@pytest.mark.parametrize("ai", [False, True])
@pytest.mark.parametrize("fee", [0., 1.])
def test_unchanged_whole_purchase_survives_three_serialized_cost_passes(ai, fee):
    # Four raw-price units are affordable. Nearest-float serialization used to
    # return 93.0103673831375 < the exact 93.010367383137508 principal, causing
    # three, then two units on the manager's later deterministic / AI checks.
    profile = {**ZERO_COSTS, "fixed_fee": fee}
    source = [purchase(ai=ai)]
    original = deepcopy(source)
    prefix = "ai_" if ai else ""
    principal = Decimal(str(RAW_PRICE)) * 4
    debit = (principal + Decimal(str(fee))).quantize(Decimal(".01"), rounding=ROUND_CEILING)
    first_rows, first_summary = reprice(source, 100., profile, ai=ai)
    assert first_rows[0][f"{prefix}suggested_units"] == 4.
    amount = first_rows[0][f"{prefix}suggested_amount"]
    assert principal <= Decimal(str(amount)) <= Decimal("100")
    # The amount is the minimum usable numeric ceiling, not a cents-rounded
    # request or an extra allowance that could buy additional principal.
    assert Decimal(str(math.nextafter(amount, 0.))) < principal
    assert first_summary["estimated_cash_debit"] == float(debit)
    current = json.loads(json.dumps(first_rows, allow_nan=False))
    for _ in range(2):
        current, summary = reprice(current, 100., profile, ai=ai)
        assert current == first_rows
        for key in ("principal", "estimated_transaction_cost", "estimated_cash_debit",
                    "rounding_allowance", "cash_remaining", "purchase_count"):
            assert summary[key] == first_summary[key]
        current = json.loads(json.dumps(current, allow_nan=False))
    assert source == original


def test_existing_rounded_down_principal_still_removes_the_unauthorized_lot():
    source = [purchase(amount=93.0103673831375)]
    first, _ = reprice(source, 100., ZERO_COSTS)
    assert first[0]["suggested_units"] == 3.
    assert first[0]["suggested_amount"] <= source[0]["suggested_amount"]
    for _ in range(5):
        later, _ = reprice(first, 100., ZERO_COSTS)
        assert later == first
        first = later


def test_fractional_purchase_does_not_lose_another_unit_step_on_each_check():
    source = [purchase(price=87.36069921854862, amount=128.93163571134722,
                       units=1.4758539808478568, whole=False)]
    first, _ = reprice(source, 1000., ZERO_COSTS)
    assert first[0]["suggested_units"] == 1.475853980847
    for _ in range(5):
        later, _ = reprice(first, 1000., ZERO_COSTS)
        assert later == first
        first = later


@pytest.mark.parametrize("ai", [False, True])
def test_explicit_later_reduction_overrides_any_previous_pricing_fields(ai):
    prefix = "ai_" if ai else ""
    first, _ = reprice([purchase(ai=ai)], 100., ZERO_COSTS, ai=ai)
    first[0][f"{prefix}suggested_amount"] = 40.
    first[0][f"{prefix}suggested_units"] = 4.
    # Existing or caller-invented decimal metadata cannot authorize more money.
    first[0]["exact_principal"] = "1000"
    first[0]["execution_principal_decimal"] = "1000"
    later, _ = reprice(first, 100., ZERO_COSTS, ai=ai)
    assert later[0][f"{prefix}suggested_units"] == 1.
    assert later[0][f"{prefix}suggested_amount"] <= 40.
    later[0][f"{prefix}suggested_units"] = 0.
    empty, _ = reprice(later, 100., ZERO_COSTS, ai=ai)
    assert empty[0][f"{prefix}suggested_units"] == 0.


def test_decimal_only_cap_is_narrowed_when_no_float_can_cover_exact_lot_principal():
    cap = Decimal("69.757775537353131")
    source = [purchase(amount=cap, units=3.)]
    first, _ = reprice(source, 100., ZERO_COSTS)
    # Neither adjacent float can both cover three exact-price units and remain
    # within this Decimal-only cap. Reducing is preferable to widening it.
    assert first[0]["suggested_units"] == 2.
    principal = Decimal(str(RAW_PRICE)) * Decimal(str(first[0]["suggested_units"]))
    assert principal <= Decimal(str(first[0]["suggested_amount"])) <= cap
    second, _ = reprice(first, 100., ZERO_COSTS)
    assert second == first


@pytest.mark.parametrize("seed", range(24))
@pytest.mark.parametrize("ai", [False, True])
def test_returned_numeric_bounds_survive_repricing_and_match_exact_cash_oracle(seed, ai):
    rng = random.Random(seed)
    budget = rng.randint(10000, 1000000) / 100
    reserve = rng.randint(0, int(budget * 50)) / 100
    profile = {**ZERO_COSTS, "fixed_fee": rng.randint(0, 250) / 100,
               "commission_pct": rng.randint(0, 200) / 100,
               "spread_bps": rng.randint(0, 50), "fx_bps": rng.randint(0, 50)}
    source = []
    for index in range(1 + rng.randrange(4)):
        raw_price = rng.uniform(.0001, 400.)
        whole = bool(index % 2)
        units = float(rng.randint(1, 30)) if whole else rng.uniform(.00001, 30.)
        # Ceilings need not equal marked principal, and the original ones stay
        # authoritative even when floating multiplication rounds downward.
        source.append(purchase(price=raw_price, amount=raw_price * units,
                               units=units, whole=whole, ai=ai))
    original = deepcopy(source)
    prefix = "ai_" if ai else ""
    first, summary = reprice(source, budget, profile, ai=ai, reserve=reserve)
    rate = Decimal(str(profile["commission_pct"])) / 100 + (
        Decimal(str(profile["spread_bps"])) + Decimal(str(profile["fx_bps"]))) / 10000
    total_debit = Decimal(0)
    for before, after in zip(source, first, strict=True):
        units = Decimal(str(after[f"{prefix}suggested_units"]))
        principal = units * Decimal(str(before["portfolio_price"]))
        amount = Decimal(str(after[f"{prefix}suggested_amount"]))
        assert principal <= amount <= Decimal(str(before[f"{prefix}suggested_amount"]))
        assert units <= Decimal(str(before[f"{prefix}suggested_units"]))
        if before["whole_units_only"]:
            assert units == units.to_integral_value()
        debit = ((principal + Decimal(str(profile["fixed_fee"])) + principal * rate)
                 .quantize(Decimal(".01"), rounding=ROUND_CEILING)) if units else Decimal(0)
        assert after[f"{prefix}estimated_cash_debit"] == float(debit)
        total_debit += debit
    assert summary["estimated_cash_debit"] == float(total_debit)
    assert total_debit + Decimal(str(reserve)) <= Decimal(str(budget))
    current = json.loads(json.dumps(first, allow_nan=False))
    for _ in range(3):
        current, later_summary = reprice(current, budget, profile, ai=ai, reserve=reserve)
        for initial, later in zip(first, current, strict=True):
            for key in ("suggested_amount", "suggested_units", "estimated_cash_debit",
                        "estimated_transaction_cost", "execution_rounding_allowance"):
                assert later[f"{prefix}{key}"] == initial[f"{prefix}{key}"]
        for key in ("principal", "estimated_transaction_cost", "estimated_cash_debit",
                    "rounding_allowance", "cash_remaining", "purchase_count"):
            assert later_summary[key] == summary[key]
        current = json.loads(json.dumps(current, allow_nan=False))
    assert source == original
