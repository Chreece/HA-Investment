from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import math
import sys

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "investment"


def load_module(name, filename):
    spec = spec_from_file_location(name, ROOT / filename)
    module = module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


indication = load_module("investment_indication_test", "indication.py")
analyze_prices = indication.analyze_prices
allocate_budget = indication.allocate_budget
sanitize_ai_ranking = indication.sanitize_ai_ranking
balanced_discovery_sample = indication.balanced_discovery_sample


def weekly_trend(count=260, start=100.0, weekly_growth=0.002):
    return [start * ((1.0 + weekly_growth) ** i) for i in range(count)]


def test_long_horizon_windows_use_weekly_elapsed_time_equivalents():
    windows = indication._sampling_windows("very_long")
    assert windows["sampling"] == "weekly"
    assert windows["r63"] == 13
    assert windows["r126"] == 26
    assert windows["r252"] == 52
    assert windows["r1260"] == 260
    assert windows["periods_per_year"] == 52.0


def test_complete_five_year_weekly_history_has_high_data_reliability():
    result = analyze_prices(
        weekly_trend(261),
        category="etf",
        risk_tolerance="medium",
        horizon="very_long",
        strategy="trend",
    )
    assert result.metrics["sampling"] == "weekly"
    assert result.metrics["history_target_observations"] == 260.0
    assert result.confidence >= 0.90
    assert result.metrics["confidence_semantics"] == "historical_data_reliability"


def test_weekly_live_quote_replaces_last_observation_instead_of_faking_new_week():
    prices = weekly_trend(260)
    result = analyze_prices(
        prices,
        current_price=prices[-1] * 1.01,
        category="etf",
        risk_tolerance="medium",
        horizon="very_long",
        strategy="trend",
    )
    assert result.metrics["history_observations"] == 260


def test_short_weekly_history_does_not_get_full_long_horizon_confidence():
    result = analyze_prices(
        weekly_trend(100),
        category="etf",
        risk_tolerance="medium",
        horizon="very_long",
        strategy="trend",
    )
    assert result.confidence < 0.80
    assert "limited_history" in result.warnings


def test_very_low_risk_crypto_is_suitability_blocked_even_with_positive_trend():
    result = analyze_prices(
        weekly_trend(261, start=1000.0, weekly_growth=0.006),
        category="crypto",
        risk_tolerance="very_low",
        horizon="very_long",
        strategy="adaptive",
    )
    assert result.metrics["suitability_eligible"] is False
    assert result.metrics["category_cap_fraction"] == 0.0
    assert "asset_class_not_suitable_for_selected_risk" in result.metrics["suitability_blockers"]
    assert result.label == "caution"
    assert result.score <= 47.0


def test_absolute_volatility_budget_is_not_relative_to_crypto_category():
    prices = [1000.0]
    # Alternating weekly moves create high absolute volatility while remaining
    # below the old 80% crypto category reference.
    for i in range(1, 261):
        move = 0.065 if i % 2 else -0.050
        prices.append(prices[-1] * (1.0 + move))
    result = analyze_prices(
        prices,
        category="crypto",
        risk_tolerance="very_low",
        horizon="very_long",
        strategy="risk_adjusted",
    )
    assert result.metrics["annualized_volatility"] is not None
    assert result.metrics["volatility_ratio"] > 1.0
    assert result.metrics["category_volatility_ratio"] < 1.0
    assert result.metrics["suitability_eligible"] is False


def test_drawdown_between_18_and_30_percent_is_penalized():
    prices = weekly_trend(220, start=100.0, weekly_growth=0.003)
    peak = prices[-1]
    # Finish 25% below the recent peak; old v6 had a dead zone here.
    prices.extend([peak * 0.95, peak * 0.88, peak * 0.80, peak * 0.75])
    result = analyze_prices(
        prices,
        category="etf",
        risk_tolerance="very_low",
        horizon="very_long",
        strategy="risk_adjusted",
    )
    assert result.metrics["current_drawdown"] <= -0.24
    assert result.metrics["drawdown_risk_ratio"] > 1.0
    assert "drawdown_high_for_selected_risk" in result.warnings


def test_very_low_profile_does_not_use_staged_escape_hatch():
    item = {
        "provider": "yahoo",
        "provider_id": "SAFE",
        "symbol": "SAFE",
        "category": "etf",
        "score": 62.52,
        "confidence": 0.94,
        "price": 100.0,
        "portfolio_price": 100.0,
        "metrics": {
            "regime_score": 0.2,
            "volatility_ratio": 0.8,
            "suitability_eligible": True,
        },
    }
    allocation = allocate_budget(
        [item],
        10_000,
        risk_tolerance="very_low",
        diversification="medium",
        min_confidence=0.45,
    )
    assert item["allocation_tier"] == "none"
    assert item["suggested_amount"] == 0.0
    assert allocation["deployed"] == 0.0
    assert allocation["staged_allocations_allowed"] is False


def test_whole_unit_mode_never_overshoots_risk_target():
    item = {
        "provider": "yahoo",
        "provider_id": "EXPENSIVE",
        "symbol": "EXPENSIVE",
        "category": "etf",
        "score": 66.5,
        "confidence": 0.90,
        "price": 2146.86,
        "portfolio_price": 2146.86,
        "metrics": {
            "regime_score": 0.2,
            "volatility_ratio": 0.8,
            "suitability_eligible": True,
        },
    }
    allocation = allocate_budget(
        [item],
        10_000,
        risk_tolerance="very_low",
        diversification="medium",
        min_confidence=0.45,
        whole_units_only=True,
    )
    assert allocation["target_deployed"] < item["portfolio_price"]
    assert allocation["whole_unit_integer_ceiling"] == allocation["target_deployed"]
    assert item["suggested_units"] == 0.0
    assert allocation["deployed"] == 0.0
    assert allocation["whole_unit_adjustment"] <= 0.0


def test_category_risk_cap_limits_crypto_portfolio_exposure():
    items = []
    for idx in range(2):
        items.append(
            {
                "provider": "kraken",
                "provider_id": f"C{idx}",
                "symbol": f"C{idx}/EUR",
                "category": "crypto",
                "score": 100.0,
                "confidence": 1.0,
                "price": 100.0,
                "portfolio_price": 100.0,
                "metrics": {
                    "regime_score": 0.2,
                    "volatility_ratio": 0.8,
                    "suitability_eligible": True,
                },
            }
        )
    allocation = allocate_budget(
        items,
        10_000,
        risk_tolerance="high",
        diversification="medium",
        min_confidence=0.0,
    )
    assert allocation["category_max_fractions"]["crypto"] == 0.25
    assert allocation["deployed"] <= 2500.01
    assert sum(float(item["suggested_amount"]) for item in items) <= 2500.01


def test_ai_cannot_resurrect_deterministically_ineligible_candidate():
    item = {
        "provider": "kraken",
        "provider_id": "ETH/EUR",
        "symbol": "ETH/EUR",
        "category": "crypto",
        "score": 47.0,
        "confidence": 0.95,
        "price": 2000.0,
        "portfolio_price": 2000.0,
        "allocation_eligible": False,
        "metrics": {
            "suitability_eligible": False,
            "regime_score": 0.2,
            "volatility_ratio": 2.5,
        },
    }
    allocation = sanitize_ai_ranking(
        [item],
        [
            {
                "provider": "kraken",
                "provider_id": "ETH/EUR",
                "score": 99,
                "action": "consider",
                "suggested_amount": 5000,
            }
        ],
        10_000,
        risk_tolerance="very_low",
    )
    assert item["ai_action"] == "watch"
    assert item["ai_suggested_amount"] == 0.0
    assert allocation["deployed"] == 0.0


def test_discovery_keeps_breadth_but_gives_more_slots_to_core_categories():
    categories = ("etf", "stock", "crypto", "fund", "commodity", "fx", "index")
    buckets = {
        category: [{"category": category, "id": f"{category}-{i}"} for i in range(10)]
        for category in categories
    }
    selected = balanced_discovery_sample(buckets, categories, limit=20)
    counts = {category: 0 for category in categories}
    for item in selected:
        counts[item["category"]] += 1
    assert len(selected) == 20
    assert all(counts[category] >= 1 for category in categories)
    assert counts["etf"] > counts["crypto"]
    assert counts["fund"] >= counts["crypto"]


def test_whole_unit_quantization_preserves_multiple_fractional_targets():
    items = [
        {
            "provider": "yahoo", "provider_id": f"A{idx}", "symbol": f"A{idx}",
            "category": category, "score": score, "confidence": 0.95,
            "price": price, "portfolio_price": price,
            "metrics": {"regime_score": 0.2, "volatility_ratio": 0.8, "suitability_eligible": True},
        }
        for idx, (category, score, price) in enumerate((
            ("etf", 82.0, 61.0), ("fund", 79.0, 53.0), ("stock", 76.0, 47.0)
        ))
    ]
    allocation = allocate_budget(
        items, 2000, risk_tolerance="medium", diversification="medium",
        min_confidence=0.0, whole_units_only=True,
    )
    positive = [item for item in items if float(item.get("suggested_units") or 0) >= 1.0]
    assert len(positive) >= 2
    assert allocation["deployed"] <= allocation["target_deployed"] + 0.01
    assert allocation["whole_unit_adjustment"] <= 0.01


def test_region_filter_prefers_compatible_fund_listings():
    compatible = indication.candidate_region_compatible
    assert compatible({"category": "etf", "provider_id": "VWCE.DE", "name": "Vanguard FTSE All-World UCITS ETF"}, "germany")
    assert compatible({"category": "etf", "provider_id": "SXR8.DE", "name": "iShares Core S&P 500 UCITS ETF"}, "eu_eea")
    assert not compatible({"category": "fund", "provider_id": "FXAIX", "name": "Fidelity 500 Index Fund"}, "germany")
    assert compatible({"category": "fund", "provider_id": "FXAIX", "name": "Fidelity 500 Index Fund"}, "us")
    assert compatible({"category": "stock", "provider_id": "MSFT", "name": "Microsoft"}, "germany")


def test_method_identifier_is_current():
    assert indication.INDICATION_METHOD == "v13_validated_risk_invariant_sleeve_blend"
