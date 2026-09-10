from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "investment"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_module(name, filename):
    spec = spec_from_file_location(name, ROOT / filename)
    module = module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


validated = load_module("investment_validated_model_test", "validated_model.py")


def test_economic_classifier_matches_all_three_frozen_validation_universes():
    fixtures = json.loads(
        (FIXTURES / "validated_economic_sleeves.json").read_text(encoding="utf-8")
    )
    assert len(fixtures) == 75
    for row in fixtures:
        result = validated.classify_economic_exposure(row)
        assert result["economic_sleeve"] == row["expected_sleeve"], row
        assert result["allocatable"] is row["expected_allocatable"], row


def test_activation_uses_frozen_v10_interval_and_reliability_only_scales_down():
    assert validated.activation(48.0, 1.0) == 0.0
    assert validated.activation(75.0, 1.0) == 1.0
    assert validated.activation(75.0, 0.4) == 0.4
    assert validated.activation(61.5, 1.0) == 0.5


def test_v12c_corrected_blend_and_cent_projection_match_fresh_v13_sample():
    fixture = json.loads(
        (FIXTURES / "v13_v12c_parity_sample.json").read_text(encoding="utf-8")
    )
    candidates = fixture["candidates"]
    weighted = validated.corrected_blend_base_weights(candidates, fixture["risk"])
    totals = validated.sleeve_totals(weighted)
    for sleeve, expected in fixture["expected_sleeves"].items():
        assert abs(totals[sleeve] - expected) <= 2e-15

    results, projection = validated.constrained_cent_projection(
        candidates, weighted, fixture["risk"], fixture["budget"]
    )
    actual = {row["symbol"]: row["suggested_amount"] for row in results}
    assert projection["projected_total_cents"] == fixture["expected_projected_total_cents"]
    assert actual == fixture["expected_amounts"]


def test_portfolio_risk_contract_scales_only_down_and_preserves_cash_when_feasible():
    weeks = [f"2024-{index:03d}" for index in range(156)]
    risky_returns = {
        week: (0.08 if index % 2 else -0.07)
        for index, week in enumerate(weeks)
    }
    cash_returns = {week: 0.0002 for week in weeks}
    candidates = [
        {
            "symbol": "CASH",
            "economic_sleeve": "cash_like",
            "market_score": 75.0,
            "confidence": 1.0,
            "activation": 1.0,
            "risk_weekly_returns": cash_returns,
        },
        {
            "symbol": "RISKY",
            "economic_sleeve": "broad_equity",
            "market_score": 75.0,
            "confidence": 1.0,
            "activation": 1.0,
            "risk_weekly_returns": risky_returns,
        },
    ]
    pre = validated.corrected_blend_base_weights(candidates, "very_low")
    post, meta = validated.scale_down_to_risk_contract(pre, "very_low")
    pre_map = {row["symbol"]: weight for row, weight in pre}
    post_map = {row["symbol"]: weight for row, weight in post}

    assert 0.0 <= meta["risk_scale"] < 1.0
    assert post_map["CASH"] == pre_map["CASH"]
    assert post_map["RISKY"] < pre_map["RISKY"]
    assert validated.within_risk_target(meta["post"], "very_low")


def test_candidate_without_minimum_risk_history_receives_no_validated_weight():
    short_history = {f"w{i}": 0.001 for i in range(20)}
    full_history = {f"w{i}": 0.001 for i in range(60)}
    candidates = [
        {
            "symbol": "SHORT",
            "name": "Example Total Stock Market ETF",
            "category": "etf",
            "market_score": 90.0,
            "confidence": 1.0,
            "risk_weekly_returns": short_history,
        },
        {
            "symbol": "FULL",
            "name": "Example Total Stock Market ETF",
            "category": "etf",
            "market_score": 75.0,
            "confidence": 1.0,
            "risk_weekly_returns": full_history,
        },
    ]
    post, meta = validated.validated_exact_weights(candidates, "medium")
    weights = {row["symbol"]: weight for row, weight in post}
    assert "SHORT" not in weights
    assert weights.get("FULL", 0.0) > 0.0
    prepared = {row["symbol"]: row for row in meta["candidates"]}
    assert prepared["SHORT"]["activation"] == 0.0


def test_nontradable_references_are_never_activated_by_validated_core():
    for asset in (
        {"category": "fx", "symbol": "EURUSD=X", "name": "EUR USD Exchange Rate"},
        {"category": "index", "symbol": "^GSPC", "name": "S&P 500 Index"},
        {"category": "commodity", "symbol": "GC=F", "name": "Gold Futures"},
    ):
        result = validated.classify_economic_exposure(asset)
        assert result["economic_sleeve"] == "nontradable"
        assert result["allocatable"] is False


def test_scaffold_market_score_restores_portfolio_context_penalties():
    item = {
        "score": 58.0,
        "confidence": 0.5,
        "metrics": {
            "signal_score_before_suitability": 58.0,
            "concentration_penalty": 0.25,
            "overlap_score_penalty": 4.0,
            "suitability_eligible": True,
        },
    }
    # 0.5 * (12*.25 + 4) = 3.5 points restored out of market signal.
    assert validated.restore_scaffold_market_score(item) == 61.5
    assert validated.validated_market_score(item, "broad_equity") == 61.5


def test_weekly_risk_map_uses_trailing_three_year_window_and_weekly_returns():
    import datetime as dt

    start = dt.datetime(2020, 1, 3, tzinfo=dt.timezone.utc)
    points = []
    value = 100.0
    for index in range(5 * 53):
        points.append({"ts": int((start + dt.timedelta(days=7 * index)).timestamp()), "value": value})
        value *= 1.001
    risk = validated.weekly_return_map_from_points(points)
    assert 150 <= len(risk) <= 160
    assert all(abs(ret - 0.001) < 1e-12 for ret in risk.values())


def test_user_constraints_can_only_reduce_validated_weights():
    candidates = [
        {
            "provider": "yahoo", "provider_id": "A", "symbol": "A",
            "confidence": 0.9, "allocation_context_scale": 0.5,
        },
        {
            "provider": "yahoo", "provider_id": "B", "symbol": "B",
            "confidence": 0.4, "allocation_context_scale": 1.0,
        },
    ]
    before = [(candidates[0], 0.40), (candidates[1], 0.30)]
    after, meta = validated.apply_downward_weight_constraints(
        before,
        min_confidence=0.5,
        max_candidate_fraction=0.15,
        minimum_cash_reserve_fraction=0.90,
    )
    weights = {row["symbol"]: weight for row, weight in after}
    assert "B" not in weights
    assert weights["A"] <= 0.10 + 1e-15
    assert meta["post_constraint_weight"] <= 0.10 + 1e-15
    assert meta["reserve_scale"] <= 1.0


def test_projection_does_not_collapse_same_symbol_from_different_providers():
    candidates = [
        {
            "provider": "one", "provider_id": "A", "symbol": "DUP",
            "name": "US Total Stock Market ETF", "category": "etf",
            "economic_sleeve": "broad_equity", "portfolio_price": 10.0,
        },
        {
            "provider": "two", "provider_id": "A", "symbol": "DUP",
            "name": "US Total Stock Market ETF", "category": "etf",
            "economic_sleeve": "broad_equity", "portfolio_price": 10.0,
        },
    ]
    weighted = [(candidates[0], 0.10), (candidates[1], 0.20)]
    results, meta = validated.constrained_cent_projection(
        candidates, weighted, "medium", 1000.0
    )
    assert [row["suggested_amount"] for row in results] == [100.0, 200.0]
    assert meta["projected_total_cents"] == 30000


def test_whole_unit_projection_never_exceeds_fractional_validated_ceiling():
    candidates = [
        {
            "provider": "yahoo", "provider_id": "A", "symbol": "A",
            "name": "US Total Stock Market ETF", "category": "etf",
            "economic_sleeve": "broad_equity", "portfolio_price": 61.0,
            "risk_weekly_returns": {f"w{i}": 0.0 for i in range(60)},
        }
    ]
    weighted = [(candidates[0], 0.10)]
    fractional, _ = validated.production_projection(
        candidates, weighted, "medium", 1000.0, whole_units_only=False
    )
    whole, meta = validated.production_projection(
        candidates, weighted, "medium", 1000.0, whole_units_only=True
    )
    assert fractional[0]["suggested_amount"] == 100.0
    assert whole[0]["suggested_amount"] == 61.0
    assert whole[0]["suggested_units"] == 1.0
    assert whole[0]["suggested_amount"] <= fractional[0]["suggested_amount"]
    assert meta["deployed"] == 61.0


def test_full_ai_is_clamped_to_deterministic_item_ceilings():
    results = [
        {
            "provider": "yahoo", "provider_id": "A", "symbol": "A",
            "portfolio_price": 100.0, "suggested_amount": 250.0,
            "risk_weekly_returns": {f"w{i}": 0.0 for i in range(60)},
        },
        {
            "provider": "yahoo", "provider_id": "B", "symbol": "B",
            "portfolio_price": 50.0, "suggested_amount": 0.0,
        },
    ]
    allocation = validated.clamp_ai_ranking_to_deterministic(
        results,
        [
            {"provider": "yahoo", "provider_id": "A", "action": "consider", "score": 99, "suggested_amount": 900.0},
            {"provider": "yahoo", "provider_id": "B", "action": "consider", "score": 99, "suggested_amount": 900.0},
        ],
        1000.0,
        risk="medium",
    )
    assert results[0]["ai_suggested_amount"] == 250.0
    assert results[1]["ai_suggested_amount"] == 0.0
    assert results[1]["ai_action"] == "watch"
    assert allocation["deployed"] == 250.0
    assert allocation["validated_deterministic_ceiling"] == 250.0


def test_validated_exact_weights_ignore_injected_economic_sleeve():
    risk = {f"2025-W{i:02d}": 0.001 for i in range(1, 54)}
    candidate = {
        "provider": "yahoo",
        "provider_id": "EURUSD=X",
        "symbol": "EURUSD=X",
        "name": "EUR USD Exchange Rate",
        "category": "fx",
        "economic_sleeve": "cash_like",  # hostile/stale caller input
        "market_score": 100.0,
        "confidence": 1.0,
        "risk_weekly_returns": risk,
    }
    weighted, meta = validated.validated_exact_weights([candidate], "very_low")
    assert weighted == []
    assert meta["candidates"][0]["economic_sleeve"] == "nontradable"
    assert meta["candidates"][0]["activation"] == 0.0


def test_market_score_transfer_matches_frozen_v13_across_all_horizons():
    indication = load_module("investment_indication_v13_score_parity", "indication.py")
    cases = json.loads(
        (FIXTURES / "v13_market_score_parity_sample.json").read_text(encoding="utf-8")
    )
    assert len(cases) == 15
    for case in cases:
        scaffold = indication.analyze_prices(
            case["prices"],
            current_price=case["prices"][-1],
            category=case["category"],
            risk_tolerance=validated.SIGNAL_SCAFFOLD_RISK,
            horizon=case["horizon"],
            strategy="adaptive",
            holding_weight=0.0,
            category_weight=0.0,
            portfolio_overlap=0.0,
            overlap_policy="allow",
        ).as_dict()
        classification = validated.classify_economic_exposure(case)
        assert classification["economic_sleeve"] == case["expected_sleeve"]
        actual = validated.validated_market_score(
            scaffold, classification["economic_sleeve"]
        )
        assert actual == case["expected_market_score"], case


def test_full_ai_without_budget_keeps_ranking_but_never_invents_money():
    results = [
        {
            "provider": "yahoo", "provider_id": "A", "symbol": "A",
            "portfolio_price": 100.0, "suggested_amount": None,
            "allocation_eligible": True,
        }
    ]
    allocation = validated.clamp_ai_ranking_to_deterministic(
        results,
        [{"provider": "yahoo", "provider_id": "A", "action": "consider", "score": 88, "reason": "supplied evidence"}],
        None,
        risk="medium",
    )
    assert results[0]["ai_score"] == 88.0
    assert results[0]["ai_action"] == "consider"
    assert results[0]["ai_suggested_amount"] is None
    assert allocation["budget"] is None


def test_no_key_discovery_fallbacks_have_conservative_known_sleeves():
    cases = [
        ({"category": "etf", "symbol": "QQQ", "name": "Invesco QQQ Trust"}, "sector_equity"),
        ({"category": "fund", "symbol": "VFIAX", "name": "Vanguard 500 Index Fund Admiral Shares"}, "broad_equity"),
        ({"category": "fund", "symbol": "FXAIX", "name": "Fidelity 500 Index Fund"}, "broad_equity"),
    ]
    for asset, sleeve in cases:
        out = validated.classify_economic_exposure(asset)
        assert out["allocatable"] is True
        assert out["economic_sleeve"] == sleeve


def _opposed_weekly_risk_maps(count=60, magnitude=0.10):
    a = {f"w{i:03d}": (magnitude if i % 2 else -magnitude) for i in range(count)}
    b = {key: -value for key, value in a.items()}
    return a, b


def test_downward_user_constraints_recheck_risk_after_removing_a_hedge():
    a_returns, b_returns = _opposed_weekly_risk_maps()
    a = {
        "provider": "yahoo", "provider_id": "A", "symbol": "A",
        "economic_sleeve": "broad_equity", "confidence": 1.0,
        "risk_weekly_returns": a_returns,
    }
    b = {
        "provider": "yahoo", "provider_id": "B", "symbol": "B",
        "economic_sleeve": "broad_equity", "confidence": 0.10,
        "risk_weekly_returns": b_returns,
    }
    before = [(a, 0.20), (b, 0.20)]
    assert validated.within_risk_target(validated.risk_signature(before), "very_low")
    after, meta = validated.apply_downward_weight_constraints(
        before,
        risk="very_low",
        min_confidence=0.50,
    )
    assert len(after) == 1
    assert after[0][0]["symbol"] == "A"
    assert after[0][1] < 0.20
    assert validated.within_risk_target(validated.risk_signature(after), "very_low")
    assert meta["post_constraint_risk"]["risk_scale"] < 1.0


def test_whole_unit_projection_rechecks_risk_when_flooring_removes_hedge():
    a_returns, b_returns = _opposed_weekly_risk_maps()
    candidates = [
        {
            "provider": "yahoo", "provider_id": "A", "symbol": "A",
            "name": "US Total Stock Market ETF", "category": "etf",
            "economic_sleeve": "broad_equity", "portfolio_price": 100.0,
            "risk_weekly_returns": a_returns,
        },
        {
            "provider": "yahoo", "provider_id": "B", "symbol": "B",
            "name": "US Total Stock Market ETF", "category": "etf",
            "economic_sleeve": "broad_equity", "portfolio_price": 1500.0,
            "risk_weekly_returns": b_returns,
        },
    ]
    weighted = [(candidates[0], 0.10), (candidates[1], 0.10)]
    projected, meta = validated.production_projection(
        candidates,
        weighted,
        "very_low",
        10000.0,
        whole_units_only=True,
    )
    weights = [
        (item, item["suggested_amount"] / 10000.0)
        for item in projected
        if item["suggested_amount"] > 0
    ]
    assert projected[1]["suggested_amount"] == 0.0
    assert projected[0]["suggested_amount"] < 1000.0
    assert validated.within_risk_target(validated.risk_signature(weights), "very_low")
    assert meta["post_discrete_risk"]["adjusted"] is True


def test_full_ai_rechecks_risk_when_ai_removes_a_hedge():
    a_returns, b_returns = _opposed_weekly_risk_maps()
    results = [
        {
            "provider": "yahoo", "provider_id": "A", "symbol": "A",
            "portfolio_price": 100.0, "suggested_amount": 1000.0,
            "risk_weekly_returns": a_returns,
        },
        {
            "provider": "yahoo", "provider_id": "B", "symbol": "B",
            "portfolio_price": 100.0, "suggested_amount": 1000.0,
            "risk_weekly_returns": b_returns,
        },
    ]
    allocation = validated.clamp_ai_ranking_to_deterministic(
        results,
        [
            {"provider": "yahoo", "provider_id": "A", "action": "consider", "score": 95, "suggested_amount": 1000.0},
            {"provider": "yahoo", "provider_id": "B", "action": "avoid", "score": 5, "suggested_amount": 0.0},
        ],
        10000.0,
        risk="very_low",
    )
    weights = [
        (item, item["ai_suggested_amount"] / 10000.0)
        for item in results
        if item["ai_suggested_amount"] > 0
    ]
    assert results[0]["ai_suggested_amount"] < 1000.0
    assert results[1]["ai_suggested_amount"] == 0.0
    assert validated.within_risk_target(validated.risk_signature(weights), "very_low")
    assert allocation["post_ai_risk"]["adjusted"] is True


def test_discrete_guard_enforces_budget_as_well_as_risk_and_item_ceilings():
    zero_risk = {f"w{i:03d}": 0.0 for i in range(60)}
    results = [
        {
            "provider": "p", "provider_id": str(i), "symbol": str(i),
            "portfolio_price": 10.0, "suggested_amount": 6000.0,
            "risk_weekly_returns": zero_risk,
        }
        for i in range(2)
    ]
    guarded, meta = validated.enforce_discrete_risk_contract(
        results, "medium", 10000.0
    )
    assert sum(row["suggested_amount"] for row in guarded) <= 10000.0
    assert all(row["suggested_amount"] <= 6000.0 for row in guarded)
    assert meta["adjusted"] is True
