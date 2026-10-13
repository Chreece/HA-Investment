from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "investment"


def load_module():
    name = "investment_affordable_lots_test"
    spec = spec_from_file_location(name, ROOT / "affordable_lots.py")
    module = module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


lots = load_module()


def candidate(symbol, sleeve, price, weight, score, *, category="etf"):
    return {
        "provider": "yahoo",
        "provider_id": symbol,
        "symbol": symbol,
        "name": symbol,
        "category": category,
        "economic_sleeve": sleeve,
        "portfolio_price": price,
        "suggested_amount": 0.0,
        "suggested_units": 0.0,
        "allocation_weight_after_constraints": weight,
        "allocation_eligible": False,
        "whole_units_only": True,
        "market_score": score,
        "confidence": 1.0,
        "relative_strength": score,
        "metrics": {"suitability_eligible": True},
    }


def build(rows, *, budget=100.0, cap=0.45, reserve=0.0, risk=lambda _rows: True):
    return lots.build_minimum_lot_trial(
        rows,
        budget=budget,
        whole_units_only=False,
        whole_unit_categories=["etf"],
        sleeve_caps={"government_bond": 0.35, "aggregate_bond": 0.30},
        candidate_cap_fraction=cap,
        reserve_fraction=reserve,
        evidence_eligible=lambda _row: True,
        risk_acceptable=risk,
    )


def test_vanguard_bond_examples_fit_a_100_euro_budget_as_minimum_lots():
    # Values are derived from the UI example: 63.79 / 13 and 69.35 / 3.
    vggf_price = 63.79 / 13.0
    vagf_price = 69.35 / 3.0
    rows = [
        candidate("VGGF.DE", "government_bond", vggf_price, 0.06379, 58.96),
        candidate("VAGF.DE", "aggregate_bond", vagf_price, 0.06935, 56.37),
    ]

    recovered, meta = build(rows, budget=100.0)
    amounts = {row["symbol"]: row["suggested_amount"] for row in recovered}
    units = {row["symbol"]: row["suggested_units"] for row in recovered}

    assert amounts == {"VGGF.DE": 4.91, "VAGF.DE": 23.12}
    assert units == {"VGGF.DE": 1.0, "VAGF.DE": 1.0}
    assert {row["symbol"] for row in meta["selected"]} == {"VGGF.DE", "VAGF.DE"}
    assert meta["policy"] == "one_affordable_lot_per_empty_sleeve"


def test_existing_positive_allocations_are_not_rewritten():
    rows = [
        {
            **candidate("VGGF.DE", "government_bond", 63.79 / 13.0, 0.06379, 58.96),
            "suggested_amount": 63.79,
            "suggested_units": 13.0,
            "allocation_eligible": True,
        },
        {
            **candidate("VAGF.DE", "aggregate_bond", 69.35 / 3.0, 0.06935, 56.37),
            "suggested_amount": 69.35,
            "suggested_units": 3.0,
            "allocation_eligible": True,
        },
    ]

    recovered, meta = build(rows, budget=1000.0)
    assert [row["suggested_amount"] for row in recovered] == [63.79, 69.35]
    assert meta["selected"] == []


def test_explicit_candidate_cap_still_blocks_an_oversized_minimum_lot():
    rows = [
        candidate("VGGF.DE", "government_bond", 63.79 / 13.0, 0.06379, 58.96),
        candidate("VAGF.DE", "aggregate_bond", 69.35 / 3.0, 0.06935, 56.37),
    ]

    recovered, meta = build(rows, budget=100.0, cap=0.10)
    amounts = {row["symbol"]: row["suggested_amount"] for row in recovered}
    assert amounts["VGGF.DE"] == 4.91
    assert amounts["VAGF.DE"] == 0.0
    assert [row["symbol"] for row in meta["selected"]] == ["VGGF.DE"]


def test_only_the_strongest_minimum_lot_is_added_per_empty_sleeve():
    rows = [
        candidate("GOV-A", "government_bond", 5.0, 0.05, 61.0),
        candidate("GOV-B", "government_bond", 4.0, 0.04, 59.0),
    ]

    recovered, meta = build(rows)
    assert recovered[0]["suggested_units"] == 1.0
    assert recovered[1]["suggested_units"] == 0.0
    assert [row["symbol"] for row in meta["selected"]] == ["GOV-A"]


def test_literal_risk_recheck_can_reject_a_minimum_lot():
    rows = [candidate("VAGF.DE", "aggregate_bond", 69.35 / 3.0, 0.06935, 56.37)]

    recovered, meta = build(rows, risk=lambda _rows: False)
    assert recovered[0]["suggested_amount"] == 0.0
    assert meta["selected"] == []


def test_cash_reserve_is_kept_before_minimum_lot_recovery():
    rows = [candidate("VAGF.DE", "aggregate_bond", 23.12, 0.06935, 56.37)]

    recovered, meta = build(rows, budget=100.0, reserve=0.80)
    assert recovered[0]["suggested_amount"] == 0.0
    assert meta["available_after_cash_reserve"] == 20.0


def test_zero_validated_weight_is_never_promoted_to_a_purchase():
    rows = [candidate("NO-SIGNAL", "government_bond", 5.0, 0.0, 90.0)]

    recovered, meta = build(rows)
    assert recovered[0]["suggested_amount"] == 0.0
    assert meta["eligible_candidates"] == 0
