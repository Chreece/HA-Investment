from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "investment"


def load_module(name, filename):
    spec = spec_from_file_location(name, ROOT / filename)
    module = module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


context = load_module("investment_portfolio_context_test", "portfolio_context.py")


def test_identity_matches_same_listing_across_providers():
    owned = {
        "provider": "yahoo",
        "provider_id": "EUNL.DE",
        "symbol": "EUNL.DE",
        "category": "etf",
        "quantity": 3,
    }
    candidate = {
        "provider": "other_feed",
        "provider_id": "EUNL.DE",
        "symbol": "EUNL.DE",
        "category": "etf",
    }
    index = context.build_owned_identity_index([owned])
    assert context.find_owned_match(candidate, index) is owned


def test_zero_quantity_history_is_not_treated_as_currently_owned():
    sold = {
        "provider": "yahoo",
        "provider_id": "VWCE.DE",
        "symbol": "VWCE.DE",
        "category": "etf",
        "quantity": 0,
    }
    index = context.build_owned_identity_index([sold])
    assert context.find_owned_match(sold, index) is None


def test_strong_identifiers_are_supported_when_present():
    owned = {"isin": "IE00BG47KH54", "category": "etf", "quantity": 2}
    candidate = {"isin": "ie00bg47kh54", "category": "etf"}
    index = context.build_owned_identity_index([owned])
    assert context.find_owned_match(candidate, index) is owned


def test_exposure_class_is_separate_from_vehicle_type():
    world = context.exposure_profile(
        {"category": "etf", "symbol": "EUNL.DE", "name": "iShares Core MSCI World UCITS ETF"}
    )
    cash_like = context.exposure_profile(
        {"category": "etf", "symbol": "XEON.DE", "name": "Xtrackers II EUR Overnight Rate Swap UCITS ETF 1C"}
    )
    bonds = context.exposure_profile(
        {"category": "etf", "symbol": "VAGF.DE", "name": "Vanguard Global Aggregate Bond UCITS ETF EUR Hedged Accumulating"}
    )
    assert world["vehicle_type"] == "etf"
    assert world["exposure_class"] == "broad_equity"
    assert cash_like["vehicle_type"] == "etf"
    assert cash_like["exposure_class"] == "cash_like"
    assert bonds["exposure_class"] == "aggregate_bond"


def test_raw_benchmarks_and_futures_are_not_default_allocatable_wrappers():
    benchmark = context.exposure_profile(
        {"category": "index", "symbol": "^GSPC", "name": "S&P 500"}
    )
    future = context.exposure_profile(
        {"category": "commodity", "symbol": "GC=F", "name": "Gold futures"}
    )
    assert benchmark["allocatable_default"] is False
    assert future["allocatable_default"] is False


def test_standalone_market_score_restores_context_penalties():
    item = {
        "score": 61.2,
        "confidence": 0.8,
        "metrics": {
            "signal_score_before_suitability": 61.2,
            "concentration_penalty": 0.5,
            "overlap_score_penalty": 8.0,
            "suitability_eligible": True,
        },
    }
    fields = context.context_score_fields(item)
    assert fields["market_score"] == 72.4
    assert fields["portfolio_context_adjustment"] == -11.2


def test_runtime_wires_both_context_controls():
    manager_text = (ROOT / "runtime_manager.py").read_text(encoding="utf-8")
    websocket_text = (ROOT / "runtime_websocket.py").read_text(encoding="utf-8")
    frontend_text = (ROOT / "www" / "investment-panel-runtime.js").read_text(encoding="utf-8")
    init_text = (ROOT / "__init__.py").read_text(encoding="utf-8")
    for text in (manager_text, websocket_text, frontend_text):
        assert "portfolio_context" in text
        assert "existing_instruments" in text
    assert "runtime_manager" in init_text
    assert "runtime_websocket" in init_text
    assert "investment-panel-runtime.js" in init_text


def test_context_score_postprocessing_preserves_validated_market_score():
    item = {
        "score": 73.75,
        "market_score": 73.75,
        "confidence": 1.0,
        "metrics": {
            "market_score_risk_profile_invariant": True,
            "signal_score_before_suitability": 62.16,
            "concentration_penalty": 0.0,
            "overlap_score_penalty": 0.0,
        },
    }
    fields = context.context_score_fields(item)
    assert fields["market_score"] == 73.75
    assert fields["portfolio_context_adjustment"] == 0.0
