"""Independent mathematical and boundary checks for research-only policies."""

from calendar import monthrange
from copy import deepcopy
from datetime import date
from decimal import Decimal
import importlib
import math
from types import FunctionType, SimpleNamespace

import pytest

from research.chronological_validation import Costs, load_stack
from research.contribution_candidates import (
    STRATEGIES,
    _ablate_scaffold,
    completed_month_sma,
    filter_approved_rows,
    initial_weights,
    ledoit_wolf_covariance,
    transform_candidates,
)


MODEL, SCORER = load_stack(False)


def isolated_scorer(component=None, weights=None):
    """Independent oracle: run production code in a private weight namespace."""
    function = SCORER.analyze_prices
    namespace = dict(function.__globals__)
    namespace["STRATEGY_WEIGHTS"] = deepcopy(weights or SCORER.STRATEGY_WEIGHTS)
    if component:
        for strategy in namespace["STRATEGY_WEIGHTS"].values():
            strategy[component] = 0.0
    clone = FunctionType(function.__code__, namespace, function.__name__, function.__defaults__, function.__closure__)
    clone.__kwdefaults__ = dict(function.__kwdefaults__ or {})
    return clone


def price_series(length=300):
    prices = [100.0]
    for i in range(1, length):
        change = .0015 + .012 * math.sin(i * .81)
        if i >= length - 5:
            change = -.015
        prices.append(prices[-1] * (1.0 + change))
    return prices


def candidate(symbol="A", name="US Total Stock Market ETF", *, prices=None, category="etf", horizon="medium"):
    prices = prices or price_series()
    scaffold = SCORER.analyze_prices(
        prices, current_price=prices[-1], category=category,
        risk_tolerance=MODEL.SIGNAL_SCAFFOLD_RISK, horizon=horizon,
        strategy=MODEL.VALIDATED_SIGNAL_STRATEGY,
    ).as_dict()
    asset = {"provider": "fixture", "provider_id": symbol, "symbol": symbol,
             "name": name, "category": category, "currency": "EUR"}
    risk = {f"2025-W{i + 1:02d}": .001 for i in range(52)}
    result = MODEL.prepare_scored_candidate(scaffold, asset, risk)
    result.update(portfolio_price=prices[-1], price=prices[-1], research_scaffold_result=scaffold)
    return result


@pytest.mark.parametrize("component", ["rsi", "reversal"])
@pytest.mark.parametrize("horizon", ["very_short", "short", "medium", "long", "very_long"])
@pytest.mark.parametrize("length", [2, 8, 83, 300])
def test_raw_ablation_matches_independent_production_namespace(component, horizon, length):
    prices = price_series(length)
    original = candidate(prices=prices, horizon=horizon)
    saved = deepcopy(original)
    saved_weights = deepcopy(SCORER.STRATEGY_WEIGHTS)
    output, _ = transform_candidates(f"no_{component}", [original], None, "2026-01-31", MODEL)
    expected_scaffold = isolated_scorer(component)(
        prices, current_price=prices[-1], category="etf",
        risk_tolerance=MODEL.SIGNAL_SCAFFOLD_RISK, horizon=horizon,
        strategy=MODEL.VALIDATED_SIGNAL_STRATEGY,
    ).as_dict()
    expected = MODEL.prepare_scored_candidate(expected_scaffold, original, original["risk_weekly_returns"])
    assert output[0]["market_score"] == expected["market_score"]
    assert output[0]["activation"] == expected["activation"]
    assert output[0]["confidence"] == original["confidence"]
    assert original == saved
    assert SCORER.STRATEGY_WEIGHTS == saved_weights


@pytest.mark.parametrize("component", ["rsi", "reversal"])
@pytest.mark.parametrize("sign", [-1, 1])
def test_ablation_happens_before_score_saturation(component, sign):
    # Inflated private fixture weights place raw scores beyond both endpoints;
    # the production function is the oracle, with no production globals changed.
    weights = deepcopy(SCORER.STRATEGY_WEIGHTS)
    for strategy in weights.values():
        strategy["momentum"] = 500.0
    prices = [100.0 * ((1.003 if sign > 0 else .997) ** i) for i in range(300)]
    if component == "reversal":
        prices[-1] *= 1.30 if sign > 0 else .70
    original = isolated_scorer(weights=weights)(
        prices, risk_tolerance="very_high", horizon="medium", strategy="adaptive",
    ).as_dict()
    proxy = SimpleNamespace(HORIZON_PROFILES=SCORER.HORIZON_PROFILES,
                            RISK_PROFILES=SCORER.RISK_PROFILES, STRATEGY_WEIGHTS=weights)
    assert original["metrics"]["signal_score_before_suitability"] in {0.0, 100.0}
    result, detail = _ablate_scaffold(original, proxy, component)
    expected = isolated_scorer(component, weights)(
        prices, risk_tolerance="very_high", horizon="medium", strategy="adaptive",
    ).as_dict()
    assert result["score"] == expected["score"]
    assert detail["removed_raw_contribution"] != 0.0


def test_suitability_block_stays_blocked_after_ablation():
    prices = [100.0 if i < 100 else .01 for i in range(300)]
    original = candidate(prices=prices)
    assert original["metrics"]["suitability_eligible"] is False
    for strategy in ("no_rsi", "no_reversal", "no_defensive_return_bonus"):
        changed, _ = transform_candidates(strategy, [original], None, "2026-01-31", MODEL)
        assert changed[0]["market_score"] <= 47.0
        assert changed[0]["activation"] == 0.0


def test_ablation_fails_if_original_formula_no_longer_matches():
    row = candidate()
    row["research_scaffold_result"]["metrics"]["signal_score_before_suitability"] += .01
    with pytest.raises(ValueError, match="Recorded score"):
        transform_candidates("no_rsi", [row], None, "2026-01-31", MODEL)


@pytest.mark.parametrize("sleeve,name,base,vol_weight,vol_ref,dd_weight,dd_ref,direct_weight,direct_ref", [
    ("cash_like", "Overnight Money Market ETF", 52., 10., .02, 8., .03, 0., 1.),
    ("government_bond", "Government Treasury Bond ETF", 48., 10., .08, 8., .15, 8., .08),
    ("aggregate_bond", "Global Aggregate Bond ETF", 46., 9., .10, 8., .22, 10., .10),
])
def test_defensive_ablation_preserves_direct_return_and_all_other_components(
    sleeve, name, base, vol_weight, vol_ref, dd_weight, dd_ref, direct_weight, direct_ref,
):
    row = candidate(name=name, prices=[100.0 * (1.001 ** i) for i in range(300)])
    assert row["economic_sleeve"] == sleeve
    changed, detail = transform_candidates("no_defensive_return_bonus", [row], None, "2026-01-31", MODEL)
    metrics = row["metrics"]
    expected = (base + vol_weight * max(0., min(1., 1. - metrics["annualized_volatility"] / vol_ref))
                + dd_weight * max(0., min(1., 1. - abs(metrics["max_drawdown"]) / dd_ref))
                + direct_weight * max(-1., min(1., metrics["return_126d"] / direct_ref))
                + 6. * row["confidence"])
    assert changed[0]["market_score"] == round(expected, 2)
    assert changed[0]["metrics"]["return_126d"] == metrics["return_126d"]
    assert detail["candidate_diagnostics"][0]["replacement_yield_inferred"] is False
    if sleeve == "cash_like":
        assert row["market_score"] == 86.0
        # Nearly zero variance makes risk-adjusted momentum unavailable; the
        # unchanged original confidence is .95, so the retained score is75.7.
        assert changed[0]["market_score"] == 75.7
        assert row["market_score"] - 14.0 != changed[0]["market_score"]


def test_equal_baseline_has_no_hidden_composite_activation_gate():
    row = candidate()
    row.update(market_score=0., score=0., activation=0., allocation_context_scale=0.)
    changed, _ = transform_candidates("equal_sleeves", [row], None, "2026-01-31", MODEL)
    weights, _ = initial_weights("equal_sleeves", changed, "medium", MODEL)
    constrained, _ = MODEL.apply_downward_weight_constraints(
        weights, risk="medium", min_confidence=.45, max_candidate_fraction=.45,
    )
    assert len(constrained) == 1
    assert constrained[0][1] == .45
    assert row["allocation_context_scale"] == 0.0
    assert changed[0]["market_score"] is None
    assert changed[0]["research_score_semantics"] == "not_used_by_simple_benchmark"


def test_simple_whole_lot_recovery_does_not_inherit_composite_score_ranking():
    recovery = importlib.import_module(MODEL.__package__ + ".affordable_lots")
    rows = [candidate("A"), candidate("Z")]
    rows[0].update(market_score=99.0, score=99.0, relative_strength=100.0)
    rows[1].update(market_score=49.0, score=49.0, relative_strength=0.0)
    transformed, _ = transform_candidates("equal_sleeves", rows, None, "2026-01-31", MODEL)
    for row in transformed:
        row.update(suggested_amount=0., suggested_units=0., whole_units_only=True,
                   allocation_weight_after_constraints=.20, portfolio_price=30.)
    recovered, detail = recovery.build_minimum_lot_trial(
        transformed, budget=100., whole_units_only=True, whole_unit_categories=(),
        sleeve_caps=MODEL.ECONOMIC_SLEEVE_CAPS["medium"], candidate_cap_fraction=.45,
        reserve_fraction=0., evidence_eligible=lambda row: True, risk_acceptable=lambda rows: True,
    )
    assert [row["symbol"] for row in recovered if row["suggested_units"] > 0] == ["Z"]
    assert detail["selected"][0]["symbol"] == "Z"
    assert transformed[0]["research_original_market_score"] == 99.0
    assert transformed[1]["research_original_market_score"] == 49.0


def test_ablation_recomputes_relative_strength_used_by_lot_ties():
    rows = [candidate("A"), candidate("B", prices=[100. * (1.0001 ** i) for i in range(300)]),
            candidate("C", prices=[100. * (.999 ** i) for i in range(300)])]
    for row in rows:
        row["relative_strength"] = -999.
    changed, _ = transform_candidates("no_rsi", rows, None, "2026-01-31", MODEL)
    assert sorted(row["relative_strength"] for row in changed) == [0., 50., 100.]


def test_equal_targets_preserve_sleeve_caps_and_reject_bad_evidence():
    rows = [candidate(symbol=symbol) for symbol in ("A", "B", "C")]
    rows += [candidate("CASH", "Overnight Money Market ETF"),
             candidate("BOND", "Global Aggregate Bond ETF")]
    bad = candidate("BAD", "Unidentified Wrapper ETF")
    bad.update(activation=1.0, economic_sleeve="cash_like")
    rows.append(bad)
    changed, _ = transform_candidates("equal_sleeves", rows, None, "2026-01-31", MODEL)
    weights, detail = initial_weights("equal_sleeves", changed, "medium", MODEL)
    values = {row["symbol"]: weight for row, weight in weights}
    assert "BAD" not in values
    assert values["A"] == values["B"] == values["C"]
    assert values["A"] == pytest.approx(1 / 9)
    assert values["CASH"] == values["BOND"] == pytest.approx(1 / 3)
    assert math.fsum(values.values()) == pytest.approx(1.)
    assert "BAD" in detail["excluded_for_shared_eligibility"]
    low, _ = initial_weights("equal_sleeves", changed, "very_low", MODEL)
    assert sum(weight for row, weight in low if row["economic_sleeve"] == "broad_equity") <= .20 + 1e-15


def test_equal_benchmark_rejects_old_portfolio_context():
    row = candidate()
    row["metrics"]["holding_weight"] = .01
    with pytest.raises(ValueError, match="old-portfolio"):
        transform_candidates("equal_sleeves", [row], None, "2026-01-31", MODEL)


def months(prices):
    return [{"date": date(2025, month, monthrange(2025, month)[1]).isoformat(), "adjusted_close": price}
            for month, price in enumerate(prices, 1)]


def test_sma_uses_ten_completed_consecutive_months_and_ignores_future_prices():
    history = months(range(101, 111))
    result = completed_month_sma(history, "2025-10-31")
    assert result["eligible"] is True
    assert result["sma"] == 105.5
    assert completed_month_sma(history + [{"date": "2025-11-01", "adjusted_close": "future invalid"}],
                               "2025-10-31") == result
    incomplete = completed_month_sma(history + [{"date": "2025-11-15", "adjusted_close": 1e9}], "2025-11-15")
    assert incomplete["sma"] == 105.5
    assert incomplete["latest_completed_month"] == "2025-10"
    assert completed_month_sma(months([100.] * 10), "2025-10-31")["eligible"] is False


def test_sma_missing_and_stale_months_fail_closed():
    history = months(range(101, 111))
    missing = completed_month_sma(history[:3] + history[4:], "2025-10-31")
    assert missing["missing_months"] == ["2025-04"]
    assert missing["eligible"] is False
    history[3]["date"] = "2025-04-01"
    stale = completed_month_sma(history, "2025-10-31")
    assert stale["stale_months"] == ["2025-04"]
    assert stale["eligible"] is False


def test_sma_filter_does_not_redistribute_failed_name_or_sleeve_targets():
    rows = [candidate("A"), candidate("B"), candidate("CASH", "Overnight Money Market ETF")]
    for row in rows:
        row["research_sma10"] = {"eligible": row["symbol"] == "A"}
        row["allocation_context_scale"] = 1.
    control, _ = initial_weights("equal_sleeves", rows, "medium", MODEL)
    trend, detail = initial_weights("sma10_equal_sleeves", rows, "medium", MODEL)
    assert [(row["symbol"], weight) for row, weight in trend] == [("A", .25)]
    assert next(weight for row, weight in control if row["symbol"] == "A") == .25
    assert detail["omitted_targets_redistributed"] is False
    assert len(detail["omitted_by_trend"]) == 2


def test_scaled_identity_shrinkage_matches_analytical_covariance_and_intensity():
    matrix = [[.1, .1], [-.1, -.1], [.2, 0.], [-.2, 0.]]
    covariance, meta = ledoit_wolf_covariance(matrix)
    # S=[[.025,.005],[.005,.005]], mu=.015, beta=.000075,
    # delta=.00025, lambda=.3, all obtained independently by hand.
    assert meta["shrinkage"] == pytest.approx(.3)
    assert covariance[0] == pytest.approx([.022, .0035])
    assert covariance[1] == pytest.approx([.0035, .008])
    diagonal, meta2 = ledoit_wolf_covariance([[.1, 0.], [-.1, 0.], [0., .2], [0., -.2]])
    assert meta2["shrinkage"] == pytest.approx(17 / 18)
    assert diagonal[0] == pytest.approx([29 / 2400, 0.])
    assert diagonal[1] == pytest.approx([0., 31 / 2400])


def test_shrinkage_is_order_invariant_centered_and_scale_equivariant():
    matrix = [[.1, .1], [-.1, -.1], [.2, 0.], [-.2, 0.]]
    original, meta = ledoit_wolf_covariance(matrix)
    for changed in (list(reversed(matrix)), [[x + .05, y - .03] for x, y in matrix]):
        calculated, info = ledoit_wolf_covariance(changed)
        for row, expected in zip(calculated, original):
            assert row == pytest.approx(expected)
        assert info["shrinkage"] == pytest.approx(meta["shrinkage"])
    scaled, info = ledoit_wolf_covariance([[2 * value for value in row] for row in matrix])
    for row, expected in zip(scaled, original):
        assert row == pytest.approx([4 * value for value in expected])
    assert info["shrinkage"] == pytest.approx(meta["shrinkage"])
    assert ledoit_wolf_covariance([[.1, .2]] * 5)[0] == [[0., 0.], [0., 0.]]


@pytest.mark.parametrize("bad", [[], [[0.]], [[0.], [0., 1.]], [[True], [0.]],
                                      [[float("nan")], [0.]], [[-1.01], [0.]]])
def test_shrinkage_rejects_invalid_data(bad):
    with pytest.raises(ValueError):
        ledoit_wolf_covariance(bad)


def test_shrinkage_challenger_is_downward_only_and_preserves_original_risk_gate():
    rows = [candidate("A"), candidate("B")]
    values = [.9, -.9, .05, -.05] * 13
    rows[0]["risk_weekly_returns"] = {f"2025-W{i + 1:02d}": value for i, value in enumerate(values)}
    rows[1]["risk_weekly_returns"] = {f"2025-W{i + 1:02d}": -value for i, value in enumerate(values)}
    control, _ = initial_weights("baseline_current", rows, "very_low", MODEL)
    challenger, detail = initial_weights("ledoit_wolf_cap", rows, "very_low", MODEL)
    before = {row["symbol"]: weight for row, weight in control}
    assert 0 < detail["shrinkage"]["scale"] < 1
    assert all(0 < weight <= before[row["symbol"]] for row, weight in challenger)
    assert MODEL.within_risk_target(MODEL.risk_signature(challenger), "very_low")


def order(principal, symbol="A", price=1.):
    return {"symbol": symbol, "suggested_amount": float(principal),
            "suggested_units": float(Decimal(str(principal)) / Decimal(str(price))),
            "portfolio_price": price}


def test_cost_cap_uses_exact_cash_rounding_and_inclusive_one_percent_boundary():
    rows = [order(111.99, "FAIL"), order(112., "BOUNDARY"), order(250., "PASS")]
    saved = deepcopy(rows)
    changed, detail = filter_approved_rows("cost_cap_1pct", rows, Costs(1., 0., 10.))
    assert [row["suggested_amount"] for row in changed] == [0., 112., 250.]
    assert detail["removed"][0]["symbol"] == "FAIL"
    assert detail["examined"][1]["cost_fraction"] == .01
    assert detail["requires_post_filter_risk_validation"] is True
    assert rows == saved
    repeated, second = filter_approved_rows("cost_cap_1pct", changed, Costs(1., 0., 10.))
    assert repeated == changed
    assert second["removed"] == []


def test_cost_cap_recomputes_friction_and_includes_rounding_even_without_fees():
    misleading = order(20.)
    misleading.update(estimated_transaction_cost=0., estimated_cash_debit=20.)
    changed, _ = filter_approved_rows("cost_cap_1pct", [misleading], Costs(1., 0., 10.))
    assert changed[0]["suggested_units"] == 0.
    tiny = order(.010001)
    changed, info = filter_approved_rows("cost_cap_1pct", [tiny], Costs(0., 0., 0.))
    assert changed[0]["suggested_units"] == 0.
    assert info["removed"][0]["all_in_cost"] > .009


def test_cost_cap_rejects_unknown_fees_and_inconsistent_purchase_authority():
    with pytest.raises(ValueError, match="confirmed"):
        filter_approved_rows("cost_cap_1pct", [order(20.)], None)
    row = order(20.)
    row["suggested_units"] = 21.
    with pytest.raises(ValueError, match="authority"):
        filter_approved_rows("cost_cap_1pct", [row], Costs())
    row["suggested_units"] = 0.
    with pytest.raises(ValueError, match="inconsistent"):
        filter_approved_rows("cost_cap_1pct", [row], Costs())


def test_baseline_is_exact_passthrough_and_cash_control_has_no_orders():
    rows = [candidate()]
    saved = deepcopy(rows)
    transformed, _ = transform_candidates("baseline_current", rows, None, "2026-01-31", MODEL)
    assert transformed == saved
    actual, _ = initial_weights("baseline_current", transformed, "medium", MODEL)
    expected, _ = MODEL.validated_exact_weights(rows, "medium")
    assert actual == expected
    assert initial_weights("cash_only", rows, "medium", MODEL)[0] == []
    approved = [order(20.)]
    assert filter_approved_rows("baseline_current", approved, None)[0] == approved
    assert "cash_only" in STRATEGIES
