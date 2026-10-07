"""Independent cash/lot oracles for the confirmed-cost purchase projection."""
from copy import deepcopy
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from importlib.util import module_from_spec, spec_from_file_location
import json
import math
from pathlib import Path
import random

import pytest

SPEC = spec_from_file_location(
    "execution_costs_subject",
    Path(__file__).resolve().parents[1] / "custom_components/investment/execution_costs.py",
)
costs = module_from_spec(SPEC)
SPEC.loader.exec_module(costs)


def profile(**changes):
    return {"confirmed": True, "fixed_fee": 0.0, "commission_pct": 0.0,
            "spread_bps": 0.0, "fx_bps": 0.0, **changes}


def row(price=10, amount=100, units=10, **changes):
    return {"provider": "example", "provider_id": "TEST", "portfolio_price": price,
            "suggested_amount": amount, "suggested_units": units,
            "allocation_eligible": True, **changes}


def test_confirmed_zero_cost_is_explicit_and_keeps_whole_purchase():
    original = [row()]
    output, summary = costs.apply_execution_costs(original, 100, profile())
    assert output[0]["suggested_units"] == 10
    assert output[0]["suggested_amount"] == 100
    assert summary["estimated_transaction_cost"] == 0
    assert summary["estimated_cash_debit"] == 100
    assert summary["cash_remaining"] == 0
    assert original == [row()]
    json.dumps(summary, allow_nan=False)


@pytest.mark.parametrize("raw", [None, {}, False, True, [], "zero", profile(confirmed=False),
                                profile(confirmed=1), profile(confirmed="true")])
def test_unknown_or_invalid_costs_never_create_a_zero_cost_purchase(raw):
    output, summary = costs.apply_execution_costs([row()], 100, raw)
    assert output[0]["suggested_units"] == 0
    assert output[0]["suggested_amount"] == 0
    assert output[0]["allocation_eligible"] is False
    assert summary["confirmed"] is False
    assert summary["cash_remaining"] == 100
    assert summary["reasons"]


@pytest.mark.parametrize("field", ["fixed_fee", "commission_pct", "spread_bps", "fx_bps"])
@pytest.mark.parametrize("value", [None, True, False, -0.01, math.nan, math.inf, -math.inf, "0", "", 10**40])
def test_all_cost_terms_require_bounded_finite_nonnegative_numbers(field, value):
    output, summary = costs.apply_execution_costs([row()], 100, profile(**{field: value}))
    assert output[0]["suggested_amount"] == 0
    assert summary["status"] == "invalid"
    assert f"execution_costs_{field}_invalid" in summary["reasons"]


@pytest.mark.parametrize("budget", [True, False, -1, math.nan, math.inf, "100", 10**40])
def test_invalid_budget_cannot_become_spendable_cash(budget):
    output, summary = costs.apply_execution_costs([row()], budget, profile())
    assert output[0]["suggested_amount"] == 0
    assert "execution_budget_invalid" in summary["reasons"]


@pytest.mark.parametrize("reserve", [True, False, -1, math.nan, math.inf, "10", 101])
def test_invalid_or_unaffordable_reserve_blocks_purchase(reserve):
    output, summary = costs.apply_execution_costs([row()], 100, profile(), reserve_amount=reserve)
    assert output[0]["suggested_amount"] == 0
    assert summary["status"] == "invalid"


def test_fixed_fee_and_all_percent_terms_are_in_cash_debit():
    output, summary = costs.apply_execution_costs(
        [row(price=10, amount=80, units=8)], 100,
        profile(fixed_fee=1, commission_pct=1, spread_bps=25, fx_bps=50),
    )
    # 80 + 1 fixed + 0.80 commission + 0.20 spread + 0.40 FX.
    assert summary["principal"] == 80
    assert summary["estimated_transaction_cost"] == pytest.approx(2.4)
    assert summary["estimated_cash_debit"] == 82.4
    assert summary["cash_remaining"] == 17.6
    assert output[0]["estimated_cash_debit"] == 82.4


def test_fee_is_charged_once_per_positive_purchase():
    purchases = [row(price=1, units=25, amount=25, provider_id=str(i)) for i in range(4)]
    purchases.append(row(amount=0, units=0))
    output, summary = costs.apply_execution_costs(purchases, 100, profile(fixed_fee=1))
    assert summary["purchase_count"] == 4
    assert summary["principal"] == pytest.approx(96)
    assert summary["estimated_transaction_cost"] == pytest.approx(4)
    assert summary["estimated_cash_debit"] == 100
    assert output[-1]["estimated_transaction_cost"] == 0


def test_reserve_is_protected_after_costs_and_fractional_cash_is_not_spent():
    output, summary = costs.apply_execution_costs([row()], 100.009, profile(fixed_fee=1), reserve_amount=10.001)
    assert summary["estimated_cash_debit"] <= 89.99
    assert summary["cash_remaining"] >= 10.001
    assert output[0]["suggested_amount"] < 89


def test_fractional_units_are_bounded_without_borrowing_a_more_favorable_price():
    output, _ = costs.apply_execution_costs([row(price=3, amount=10, units=3)], 100, profile())
    assert output[0]["suggested_units"] == 3
    assert output[0]["suggested_amount"] == 9
    output, _ = costs.apply_execution_costs([row(portfolio_price=0, price=1)], 100, profile())
    assert output[0]["suggested_amount"] == 0


@pytest.mark.parametrize("blocked", ["allocation_eligible", "instrument_identity_eligible", "data_quality_eligible"])
def test_failed_eligibility_cannot_be_resurrected_by_cost_solver(blocked):
    output, _ = costs.apply_execution_costs([row(**{blocked: False})], 100, profile())
    assert output[0]["suggested_amount"] == output[0]["suggested_units"] == 0


@pytest.mark.parametrize("field", ["suggested_amount", "suggested_units", "portfolio_price"])
@pytest.mark.parametrize("value", [True, math.inf, math.nan, -1, "10", None])
def test_invalid_purchase_inputs_fail_closed(field, value):
    output, _ = costs.apply_execution_costs([row(**{field: value})], 100, profile())
    assert output[0]["suggested_amount"] == output[0]["suggested_units"] == 0


def test_whole_unit_boundary_uses_exact_price_and_conservative_cash_rounding():
    # The price cannot be rounded down to 33.33 to squeeze three units into 100.
    output, summary = costs.apply_execution_costs(
        [row(price=33.334, amount=100.002, units=3, whole_units_only=True)], 100, profile(),
    )
    assert output[0]["suggested_units"] == 2
    assert output[0]["suggested_amount"] == 66.668
    assert output[0]["estimated_cash_debit"] == 66.67
    assert summary["rounding_allowance"] == pytest.approx(0.002)


def test_rounded_down_upstream_principal_does_not_authorize_extra_principal():
    output, _ = costs.apply_execution_costs(
        [row(price=10.004, amount=10, units=1, whole_units_only=True)], 100, profile(),
    )
    assert output[0]["suggested_units"] == 0
    assert output[0]["execution_cost_blockers"] == ["execution_costs_no_affordable_purchase"]


def test_whole_unit_fee_boundary_matches_exhaustive_single_asset_oracle():
    for price in [0.013, 1.004, 10, 33.334, 99, 100]:
        for fee in [0, 0.001, 1, 7]:
            for commission in [0, 0.25, 2]:
                approved_units = 10
                cap = float(Decimal(str(price)) * approved_units)
                output, summary = costs.apply_execution_costs(
                    [row(price, cap, approved_units, whole_units_only=True)],
                    100, profile(fixed_fee=fee, commission_pct=commission),
                )
                affordable = []
                for units in range(approved_units + 1):
                    principal = Decimal(str(price)) * units
                    debit = ((principal * (1 + Decimal(str(commission)) / 100) + Decimal(str(fee)))
                             .quantize(Decimal("0.01"), rounding=ROUND_CEILING)) if units else 0
                    if debit <= 100:
                        affordable.append(units)
                assert output[0]["suggested_units"] == max(affordable)
                assert summary["estimated_cash_debit"] <= 100


def test_repricing_after_risk_or_ai_reduction_never_restores_previous_size():
    original = [row(ai_suggested_amount=50, ai_suggested_units=5)]
    output, _ = costs.apply_execution_costs(original, 100, profile(fixed_fee=1),
                                          amount_key="ai_suggested_amount", units_key="ai_suggested_units")
    assert output[0]["suggested_amount"] == 100
    assert output[0]["ai_suggested_amount"] == 50
    assert output[0]["ai_estimated_cash_debit"] == 51
    output[0]["ai_suggested_amount"] = 20
    output[0]["ai_suggested_units"] = 2
    reduced, summary = costs.apply_execution_costs(output, 100, profile(fixed_fee=1),
                                                amount_key="ai_suggested_amount", units_key="ai_suggested_units")
    assert reduced[0]["ai_suggested_amount"] == 20
    assert summary["estimated_cash_debit"] == 21


def test_no_budget_leaves_analysis_rows_and_unknown_costs_visible():
    original = [row(amount=None, units=None)]
    output, summary = costs.apply_execution_costs(original, None, None)
    assert output[0]["suggested_amount"] is None
    assert output[0]["estimated_cash_debit"] is None
    assert summary["status"] == "not_requested"
    assert summary["profile"]["status"] == "unknown"


def test_generated_portfolios_obey_independent_decimal_cash_and_no_increase_contracts():
    rng = random.Random(6117)
    for _ in range(120):
        budget = rng.randrange(1, 30000) / 100
        reserve = rng.randrange(0, int(budget * 100) + 1) / 100
        raw = profile(fixed_fee=rng.randrange(0, 200) / 100,
                      commission_pct=rng.randrange(0, 300) / 100,
                      spread_bps=rng.randrange(0, 51), fx_bps=rng.randrange(0, 101))
        source = []
        for index in range(rng.randrange(1, 6)):
            price = rng.randrange(1, 100000) / 1000
            units = rng.randrange(1, 100) / (1 if index % 2 else 10)
            principal = float(Decimal(str(price)) * Decimal(str(units)))
            source.append(row(price, principal, units, whole_units_only=bool(index % 2)))
        frozen = deepcopy(source)
        output, summary = costs.apply_execution_costs(source, budget, raw, reserve_amount=reserve)
        debit_total = Decimal(0)
        rate = Decimal(str(raw["commission_pct"])) / 100 + (
            Decimal(str(raw["spread_bps"])) + Decimal(str(raw["fx_bps"]))) / 10000
        for before, after in zip(source, output, strict=True):
            units = Decimal(str(after["suggested_units"]))
            principal = units * Decimal(str(before["portfolio_price"]))
            assert after["suggested_units"] <= before["suggested_units"]
            assert after["suggested_amount"] <= before["suggested_amount"] + 1e-11
            assert after["suggested_amount"] == pytest.approx(float(principal), abs=1e-11)
            if before["whole_units_only"]:
                assert units == units.to_integral_value()
            debit = (principal + Decimal(str(raw["fixed_fee"])) + principal * rate).quantize(
                Decimal("0.01"), rounding=ROUND_CEILING) if units else Decimal(0)
            assert after["estimated_cash_debit"] == float(debit)
            debit_total += debit
        assert debit_total + Decimal(str(reserve)) <= Decimal(str(budget))
        assert summary["estimated_cash_debit"] == float(debit_total)
        assert source == frozen
