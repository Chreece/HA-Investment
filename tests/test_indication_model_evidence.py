"""Preserve live evidence from preparation through allocation and AI ceilings."""

import sys
from copy import deepcopy
from datetime import date, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "custom_components/investment/validated_model.py"
SPEC = spec_from_file_location("indication_model_evidence_subject", SOURCE)
model = module_from_spec(SPEC)
sys.modules[SPEC.name] = model
SPEC.loader.exec_module(model)

CONTRACT = "source-time-and-exact-identity-v1"


def weekly_returns():
    result = {}
    for index in range(156):
        current = date(2023, 10, 9) + timedelta(weeks=index)
        year, week, _ = current.isocalendar()
        result[f"{year}-W{week:02d}"] = .001 if index % 2 else -.001
    return result


def fund():
    """Exact EUR Xetra listing of the issuer catalog's USD accumulating class."""
    return {
        "provider": "yahoo", "provider_id": "SXR8.DE", "symbol": "SXR8.DE",
        "name": "iShares Core S&P 500 UCITS ETF USD Accumulating",
        "category": "etf", "currency": "EUR", "exchange": "Xetra",
        "isin": "IE00B5BMR087", "share_class": "USD Accumulating",
        "share_class_currency": "USD",
        "instrument_metadata": {
            "provider": "yahoo", "provider_id": "SXR8.DE", "symbol": "SXR8.DE",
            "category": "etf", "currency": "EUR", "exchange": "GER",
        },
        "evidence_as_of_date": "2026-10-06",
        "input_evidence_contract": CONTRACT,
        "data_quality_eligible": True,
        "instrument_identity_eligible": True,
    }


def prepare(asset=None, *, scaffold=None, price=25):
    if asset is None:
        asset = fund()
    if scaffold is None:
        scaffold = {"score": 80, "confidence": 1.0, "metrics": {}}
    result = model.prepare_scored_candidate(scaffold, asset, weekly_returns())
    result.update(price=price, portfolio_price=price)
    return result


def project(item, *, whole=False):
    weighted, _ = model.validated_exact_weights([item], "very_high")
    rows, meta = model.production_projection([item], weighted, "very_high", 1000, whole_units_only=whole)
    return weighted, rows, meta


@pytest.mark.parametrize("whole", [False, True])
def test_actual_exact_fund_evidence_survives_every_core_pass(whole):
    asset = fund()
    original = deepcopy(asset)
    prepared = prepare(asset)
    assert prepared["activation"] > 0
    assert prepared["economic_sleeve"] == "broad_equity"
    assert prepared["instrument_metadata"] == asset["instrument_metadata"]
    weighted, rows, meta = project(prepared, whole=whole)
    assert any(weight > 0 for _, weight in weighted)
    assert meta["deployed"] > 0
    assert rows[0]["suggested_amount"] > 0
    assert rows[0]["economic_sleeve"] == "broad_equity"
    assert asset == original


@pytest.mark.parametrize("field", ["data_quality_eligible", "instrument_identity_eligible"])
@pytest.mark.parametrize("value", [False, None, "true", 1])
@pytest.mark.parametrize("whole", [False, True])
def test_failed_or_non_boolean_evidence_cannot_reenter_after_preparation(field, value, whole):
    asset = fund()
    asset[field] = value
    # Scaffold output is a different source and cannot promote live input flags.
    scaffold = {"score": 100, "confidence": 1.0, "metrics": {}, field: True}
    item = prepare(asset, scaffold=scaffold)
    assert item["activation"] == 0
    assert item[field] == value
    weighted, rows, meta = project(item, whole=whole)
    assert not any(weight > 0 for _, weight in weighted)
    assert rows[0]["suggested_amount"] == 0
    assert rows[0]["suggested_units"] == 0
    assert meta["cash_reserve"] == 1000


@pytest.mark.parametrize("missing", ["data_quality_eligible", "instrument_identity_eligible"])
def test_live_contract_requires_both_explicit_gate_results(missing):
    asset = fund()
    asset.pop(missing)
    item = prepare(asset)
    assert item["activation"] == 0
    _, rows, meta = project(item)
    assert rows[0]["suggested_amount"] == 0
    assert meta["deployed"] == 0


@pytest.mark.parametrize("missing", ["data_quality_eligible", "instrument_identity_eligible"])
def test_scaffold_cannot_fill_a_missing_live_gate_and_reactivate_later(missing):
    asset = fund()
    asset.pop(missing)
    item = prepare(asset, scaffold={"score": 100, "confidence": 1.0, "metrics": {}, missing: True})
    assert item["activation"] == 0
    weighted, rows, meta = project(item)
    assert not any(weight > 0 for _, weight in weighted)
    assert rows[0]["suggested_amount"] == 0
    assert meta["deployed"] == 0


@pytest.mark.parametrize("missing", ["instrument_metadata", "evidence_as_of_date"])
def test_scaffold_cannot_supply_missing_source_identity_evidence(missing):
    asset = fund()
    value = asset.pop(missing)
    item = prepare(asset, scaffold={"score": 100, "confidence": 1.0, "metrics": {}, missing: value})
    assert item["activation"] == 0
    weighted, rows, meta = project(item)
    assert not any(weight > 0 for _, weight in weighted)
    assert rows[0]["suggested_amount"] == 0
    assert meta["deployed"] == 0


def test_live_verified_flags_cannot_fall_back_to_name_taxonomy_when_metadata_missing():
    asset = fund()
    asset.pop("instrument_metadata")
    asset.update(verified=True, issuer_verified=True, economic_sleeve="cash_like")
    item = prepare(asset)
    assert item["activation"] == 0
    _, rows, meta = project(item)
    assert rows[0]["suggested_amount"] == 0
    assert meta["deployed"] == 0


@pytest.mark.parametrize("override", [
    {"inverse": True},
    {"leveraged": True},
    {"leverage_factor": 2},
    {"income_treatment": "Distributing"},
    {"currency_hedged": True},
])
def test_rejected_caller_structure_assertion_is_not_lost_before_weight_recalculation(override):
    asset = fund()
    asset.update(override)
    item = prepare(asset)
    assert item["activation"] == 0
    weighted, rows, meta = project(item)
    assert not any(weight > 0 for _, weight in weighted)
    assert rows[0]["suggested_amount"] == 0
    assert meta["deployed"] == 0


def test_caller_sleeve_and_display_name_cannot_replace_exact_catalog_exposure():
    asset = fund()
    asset.update(name="Money Market Treasury", economic_sleeve="cash_like", economic_subtype="cash", verified=True)
    item = prepare(asset)
    weighted, rows, meta = project(item)
    assert item["economic_sleeve"] == "broad_equity"
    assert all(row["economic_sleeve"] == "broad_equity" for row, _ in weighted)
    assert rows[0]["economic_sleeve"] == "broad_equity"
    assert meta["deployed"] > 0


@pytest.mark.parametrize("field", ["data_quality_eligible", "instrument_identity_eligible"])
def test_projection_rejects_caller_supplied_positive_weight_with_failed_evidence(field):
    item = prepare()
    item[field] = False
    with pytest.raises(AssertionError, match="failed input evidence"):
        model.production_projection([item], [(item, .1)], "very_high", 1000)


def test_whole_lot_rescue_cannot_bypass_failed_evidence_hidden_by_cent_rounding():
    failed = prepare(price=.01)
    failed["data_quality_eligible"] = False
    approved = prepare(price=10000)
    approved.update(provider_id="EUNL.DE", symbol="EUNL.DE", isin="IE00B4L5Y983")
    approved["instrument_metadata"] = {
        **approved["instrument_metadata"], "provider_id": "EUNL.DE", "symbol": "EUNL.DE",
    }
    # A malformed caller-provided weight must fail even when its cent target
    # rounds to zero and only collective whole-lot fitting could activate it.
    weighted = [(failed, .000001), (approved, .1)]
    try:
        rows, _ = model.production_projection([failed, approved], weighted, "very_high", 1000, whole_units_only=True)
    except AssertionError:
        return  # Explicit failure is a valid fail-closed outcome.
    rejected = next(row for row in rows if row["provider_id"] == "SXR8.DE")
    assert rejected["suggested_amount"] == 0
    assert rejected["suggested_units"] == 0


@pytest.mark.parametrize("field", ["data_quality_eligible", "instrument_identity_eligible"])
@pytest.mark.parametrize("amount", [None, 1000])
def test_ai_direct_boundary_honors_failed_source_flags_even_with_stale_allocation_flag(field, amount):
    item = prepare()
    item.update(allocation_eligible=True, suggested_amount=100, suggested_units=4)
    item[field] = False
    results = [item]
    ranking = [{"provider": item["provider"], "provider_id": item["provider_id"],
                "action": "buy", "score": 100, "suggested_amount": 999999,
                "data_quality_eligible": True, "instrument_identity_eligible": True}]
    model.clamp_ai_ranking_to_deterministic(results, ranking, amount, risk="very_high")
    assert results[0]["ai_action"] == "watch"
    if amount is not None:
        assert results[0]["ai_suggested_amount"] == 0
        assert results[0]["ai_suggested_units"] == 0


def test_eligible_ai_can_reduce_positive_allocation_but_never_increase_it():
    _, rows, meta = project(prepare())
    original = rows[0]["suggested_amount"]
    assert original > 0
    ranking = [{"provider": "yahoo", "provider_id": "SXR8.DE", "action": "buy",
                "score": 100, "suggested_amount": original * 100}]
    model.clamp_ai_ranking_to_deterministic(rows, ranking, 1000, risk="very_high")
    assert rows[0]["ai_action"] == "consider"
    assert 0 < rows[0]["ai_suggested_amount"] <= original
    assert rows[0]["ai_suggested_amount"] <= meta["deployed"]
