"""Independent risk mathematics and dated-diagnostic boundary tests."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from importlib.util import module_from_spec, spec_from_file_location
import json
import math
from pathlib import Path
import random
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "investment"


def load(name, filename):
    spec = spec_from_file_location(name, ROOT / filename)
    module = module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


model = load("robustness_validated_model", "validated_model.py")
evidence = load("robustness_risk_evidence", "risk_evidence.py")


def empirical_integral_oracle(values):
    """Integrate quantile probability mass with exact rational arithmetic."""
    remaining = Fraction(1, 20)
    total = Fraction(0)
    for value in sorted(Fraction(str(number)) for number in values):
        probability = min(remaining, Fraction(1, len(values)))
        total += value * probability
        remaining -= probability
        if remaining == 0:
            break
    return float(max(Fraction(0), -total / Fraction(1, 20)))


def dated_map(values, start=date(2022, 1, 3)):
    return {(start + timedelta(weeks=index)).strftime("%G-W%V"): value
            for index, value in enumerate(values)}


def asset(values=None, *, name="A", sleeve="broad_equity", start=date(2022, 1, 3), exposure=None):
    if values is None:
        values = [(.02 if index % 4 else -.03) for index in range(156)]
    result = {"symbol": name, "economic_sleeve": sleeve,
              "risk_weekly_returns": dated_map(values, start)}
    if exposure is not None:
        result["risk_exposures"] = exposure
    return result


@pytest.mark.parametrize("count", [30, 31, 39, 40, 41, 52, 59, 60, 61, 79, 80, 99, 100, 104, 156, 159, 160, 201])
def test_exact_empirical_es_matches_independent_rational_quantile_integral(count):
    rng = random.Random(count)
    values = [rng.randrange(-800, 1000) / 1000 for _ in range(count)]
    assert model.expected_shortfall_loss(values) == pytest.approx(empirical_integral_oracle(values), abs=2e-15)
    rng.shuffle(values)
    assert model.expected_shortfall_loss(values) == pytest.approx(empirical_integral_oracle(values), abs=2e-15)


def test_es_fractional_boundary_reproduces_52_week_counterexample():
    values = [-.20, -.05, -.01] + [.01] * 49
    expected = float(Fraction(20 + 5, 100) + Fraction(6, 10) * Fraction(1, 100)) / 2.6
    assert model.expected_shortfall_loss(values) == pytest.approx(expected)
    assert expected == pytest.approx(.09846153846153846)
    assert expected > (.20 + .05 + .01) / 3


@pytest.mark.parametrize("value", [0, .0001, 1e308])
def test_positive_only_return_series_has_zero_es_loss_without_overflow(value):
    assert model.expected_shortfall_loss([value] * 52) == 0


@pytest.mark.parametrize("count", [0, 1, 29])
def test_core_es_preserves_minimum_observation_requirement(count):
    assert model.expected_shortfall_loss([-.01] * count) is None


@pytest.mark.parametrize("bad", [True, False, None, math.nan, math.inf, -math.inf, -1.000001, "bad", {}, []])
def test_es_and_drawdown_reject_every_corrupt_observation_even_after_total_loss(bad):
    values = [-1.0] + [.01] * 50 + [bad]
    assert model.expected_shortfall_loss(values) is None
    assert model.path_max_drawdown_loss(values) is None


def test_drawdown_preserves_loss_after_wealth_underflow_and_does_not_overflow_on_gains():
    assert model.path_max_drawdown_loss([-.99] * 400 + [.1] * 20) == 1.0
    assert model.path_max_drawdown_loss([1e308] * 100 + [-.5]) == pytest.approx(.5, abs=2e-12)
    assert model.path_max_drawdown_loss([.1, -1.0, 100]) == 1.0


def test_uniform_sustained_decline_is_rejected_when_user_drawdown_limit_is_provided():
    item = asset([-.01] * 52)
    signature = model.risk_signature([(item, 1.0)])
    assert signature["annualized_volatility_3y"] == 0
    assert signature["expected_shortfall_95_weekly_3y"] == pytest.approx(.01)
    assert signature["max_drawdown_3y"] == pytest.approx(1 - .99 ** 52)
    assert model.within_risk_target(signature, "very_low")
    assert not model.within_risk_target(signature, "very_low", max_drawdown_loss=.4)
    assert model.within_risk_target(signature, "very_low", max_drawdown_loss=.5)


@pytest.mark.parametrize("bad", [-.01, 1.01, True, False, math.nan, math.inf, -math.inf, "bad"])
def test_optional_drawdown_limit_invalid_values_cannot_pass(bad):
    signature = model.risk_signature([(asset([.001] * 52), .1)])
    assert not model.within_risk_target(signature, "low", max_drawdown_loss=bad)


@pytest.mark.parametrize("bad", [None, math.nan, math.inf, True, -1, 1.01])
def test_optional_drawdown_requires_actual_finite_bounded_path_statistic(bad):
    signature = model.risk_signature([(asset([.001] * 52), .1)])
    signature["max_drawdown_3y"] = bad
    assert not model.within_risk_target(signature, "low", max_drawdown_loss=.2)


def test_zero_explicit_drawdown_limit_has_defined_behavior():
    signature = model.risk_signature([(asset([.001] * 52), .1)])
    assert model.within_risk_target(signature, "low", max_drawdown_loss=0)


def test_risk_signature_extreme_finite_inputs_never_emit_nonfinite_json_or_pass():
    signature = model.risk_signature([(asset(([1e308, -.5] * 26)), 1)])
    assert signature["annualized_volatility_3y"] is None
    assert not model.within_risk_target(signature, "very_high")
    json.dumps(signature, allow_nan=False)


def test_finite_inputs_whose_weighted_arithmetic_overflows_cannot_produce_risk_signature():
    signature = model.risk_signature([(asset([1e308] * 52), 1e308)])
    assert signature["annualized_volatility_3y"] is None
    assert not model.within_risk_target(signature, "very_high")
    json.dumps(signature, allow_nan=False)


def test_dated_alignment_uses_common_observations_and_preserves_exact_joint_hedge():
    values = [.06 if index % 3 else -.08 for index in range(104)]
    left = asset(values)
    right = asset([-value for value in values], name="B")
    rows = [(left, .5), (right, .5)]
    original = deepcopy(rows)
    result = evidence.evaluate_risk_evidence(rows, as_of="2026-10-06")
    assert result["status"] == "ok"
    assert result["history"]["observations"] == 104
    assert result["windows"]["full"]["annualized_volatility"] == 0
    assert result["windows"]["full"]["expected_shortfall_95_weekly"] == 0
    assert result["windows"]["full"]["path"]["max_drawdown_loss"] == 0
    for quantiles in result["bootstrap"]["resampling_quantiles"].values():
        assert all(value == 0 for value in quantiles.values())
    assert rows == original
    json.dumps(result, allow_nan=False)


def test_dates_and_common_history_cannot_be_borrowed_from_another_position():
    left = asset([.01] * 104)
    right = asset([.01] * 104, start=date(2023, 1, 2))
    aligned = evidence.dated_portfolio_returns([(left, .3), (right, .2)])
    assert aligned["status"] == "ok"
    assert len(aligned["returns"]) == 52
    assert aligned["returns"] == pytest.approx([.005] * 52)
    assert aligned["history"]["cash_weight"] == .5
    missing = asset([])
    assert evidence.dated_portfolio_returns([(left, .3), (missing, 1e-16)])["status"] == "unavailable"
    assert evidence.dated_portfolio_returns([(left, .3), (missing, 0)])["status"] == "ok"


@pytest.mark.parametrize("raw", ["2021-W53", "2020-W00", "2020-W54", "2020-W1", "2020-001", "2020-w01", " 2020-W01", "2001_01", True])
def test_noncanonical_or_nonexistent_iso_week_keys_are_rejected(raw):
    item = asset()
    item["risk_weekly_returns"][raw] = .01
    assert evidence.dated_portfolio_returns([(item, .5)])["issues"] == ["invalid_iso_week"]


@pytest.mark.parametrize("bad", [True, False, math.nan, math.inf, -math.inf, -1.01, None, "0.01"])
def test_new_evidence_contract_rejects_corrupt_or_coerced_returns(bad):
    item = asset()
    item["risk_weekly_returns"][next(iter(item["risk_weekly_returns"]))] = bad
    result = evidence.evaluate_risk_evidence([(item, .5)], as_of="2026-10-06")
    assert result["status"] == "invalid"
    assert result["issues"] == ["invalid_weekly_return"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("bad", [True, False, math.nan, math.inf, -math.inf, -.1, 1.1, None, "0.5"])
def test_invalid_weights_never_become_cash_in_diagnostics(bad):
    result = evidence.evaluate_risk_evidence([(asset(), bad)])
    assert result["status"] == "invalid"
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("rows", [None, True, [True], [(asset(),)], [(asset(), .2, .3)], [(None, .5)]])
def test_invalid_position_shapes_are_reported_without_exceptions(rows):
    assert evidence.evaluate_risk_evidence(rows)["status"] == "invalid"


def test_overweight_portfolios_and_bounded_work_limits_are_explicitly_invalid():
    assert evidence.dated_portfolio_returns([(asset(), .7), (asset(name="B"), .4)])["issues"] == ["position_weights_exceed_one"]
    assert evidence.dated_portfolio_returns([(asset(), 0)] * 257)["issues"] == ["too_many_positions"]
    assert evidence.dated_portfolio_returns([(asset([.01] * 1041), .2)])["issues"] == ["history_exceeds_bounded_window"]


def test_all_cash_is_distinct_from_missing_invested_history():
    cash = evidence.evaluate_risk_evidence([])
    missing = evidence.evaluate_risk_evidence([(asset([]), .5)])
    assert cash["status"] == "cash_only"
    assert cash["history"]["cash_weight"] == 1
    assert missing["status"] == "unavailable"
    assert missing["history"]["cash_weight"] is None


def test_future_weeks_rejected_but_current_iso_week_can_be_partial():
    item = asset()
    item["risk_weekly_returns"]["2026-W42"] = .01
    assert evidence.dated_portfolio_returns([(item, .5)], as_of="2026-10-06")["issues"] == ["future_week"]
    del item["risk_weekly_returns"]["2026-W42"]
    item["risk_weekly_returns"]["2026-W41"] = .01
    assert evidence.dated_portfolio_returns([(item, .5)], as_of="2026-10-06")["history"]["latest_week_may_be_partial"]


@pytest.mark.parametrize("bad", ["2026-02-30", "2026/10/06", "2026-10-06T10:00:00", datetime(2026, 10, 6), True, math.nan])
def test_invalid_or_naive_as_of_is_rejected(bad):
    assert evidence.evaluate_risk_evidence([(asset(), .5)], as_of=bad)["status"] == "invalid"


def test_aware_as_of_uses_utc_date_at_week_boundary():
    item = asset()
    item["risk_weekly_returns"]["2026-W42"] = .01
    cutoff = datetime(2026, 10, 12, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    assert evidence.dated_portfolio_returns([(item, .5)], as_of=cutoff)["issues"] == ["future_week"]


def test_calendar_gaps_disable_full_path_and_prevent_fake_horizon_windows():
    values = [-.01] * 156
    item = asset(values)
    weeks = list(item["risk_weekly_returns"])
    del item["risk_weekly_returns"][weeks[78]]
    result = evidence.evaluate_risk_evidence([(item, 1.0)], as_of="2026-10-06", max_drawdown_loss=.2, horizon_weeks=104)
    assert result["status"] == "ok"
    assert result["issues"] == ["calendar_gaps"]
    assert result["history"]["calendar_gap_count"] == 1
    assert result["windows"]["full"]["path"] is None
    assert result["drawdown_limit"]["status"] == "unavailable"
    assert result["windows"]["recent_104"]["status"] == "unavailable"
    assert result["windows"]["recent_52"]["path"]["max_drawdown_loss"] == pytest.approx(1 - .99 ** 52)
    assert result["horizon"]["historical_windows"] == 0
    assert result["bootstrap"]["valid_source_block_starts"] == (78 - 8 + 1) + (77 - 8 + 1)


def test_tiny_isolated_segments_never_form_valid_bootstrap_blocks():
    item = asset()
    item["risk_weekly_returns"] = {
        (date(2022, 1, 3) + timedelta(weeks=index * 2)).strftime("%G-W%V"): -.01
        for index in range(52)
    }
    result = evidence.evaluate_risk_evidence([(item, .5)], as_of="2026-10-06")
    assert result["status"] == "ok"
    assert result["bootstrap"]["reason"] == "no_contiguous_source_blocks"
    assert result["windows"]["full"]["path"] is None


def test_drawdown_underwater_duration_and_optional_bound_from_diagnostics():
    result = evidence.evaluate_risk_evidence([(asset([-.01] * 52), 1)], as_of="2026-10-06", max_drawdown_loss=.4, horizon_weeks=52, goal_return=0)
    path = result["windows"]["full"]["path"]
    assert path["max_observed_underwater_weeks"] == 52
    assert path["current_underwater_weeks"] == 52
    assert path["unrecovered_at_end"]
    assert result["drawdown_limit"]["status"] == "breach"
    assert result["tail"]["observation_mass"] == 2.6
    assert result["tail"]["complete_observations"] == 2
    assert result["horizon"]["historical_windows"] == 1
    assert result["horizon"]["historical_goal_shortfall_count"] == 1
    assert result["horizon"]["min_horizon_return"] == pytest.approx(.99 ** 52 - 1)


def test_recovery_resets_current_underwater_duration_and_total_loss_cannot_recover():
    recovered = evidence.evaluate_risk_evidence([(asset([-.25, 1 / 3] + [0] * 50), 1)], bootstrap_repetitions=0)
    assert recovered["windows"]["full"]["path"]["current_underwater_weeks"] == 0
    assert recovered["windows"]["full"]["path"]["max_observed_underwater_weeks"] == 1
    lost = evidence.evaluate_risk_evidence([(asset([-1] + [100] * 51), 1)], bootstrap_repetitions=0)
    assert lost["windows"]["full"]["path"]["current_underwater_weeks"] == 52
    assert lost["windows"]["full"]["path"]["cumulative_return"] == -1


def test_26_week_diagnostic_does_not_invent_es_below_core_minimum():
    result = evidence.evaluate_risk_evidence([(asset(), .5)], bootstrap_repetitions=0)
    window = result["windows"]["recent_26"]
    assert window["annualized_volatility"] is not None
    assert window["expected_shortfall_95_weekly"] is None
    assert window["tail"]["observation_mass"] == 1.3
    assert window["tail"]["minimum_observations_for_es"] == 30


def test_bootstrap_reproducibility_work_bound_and_nontrivial_uncertainty():
    rng = random.Random(331)
    item = asset([rng.uniform(-.1, .1) for _ in range(156)])
    result = evidence.evaluate_risk_evidence([(item, .7)], as_of="2026-10-06")
    repeated = evidence.evaluate_risk_evidence([(item, .7)], as_of="2026-10-06")
    changed = evidence.evaluate_risk_evidence([(item, .7)], as_of="2026-10-06", bootstrap_seed=99)
    assert result == repeated
    assert result["bootstrap"]["repetitions"] == 128
    assert result["bootstrap"]["resampling_quantiles"] != changed["bootstrap"]["resampling_quantiles"]
    for quantiles in result["bootstrap"]["resampling_quantiles"].values():
        assert quantiles["lower_05"] <= quantiles["median_50"] <= quantiles["upper_95"]
    assert result["bootstrap"]["resampling_quantiles"]["max_drawdown_loss"]["lower_05"] < result["bootstrap"]["resampling_quantiles"]["max_drawdown_loss"]["upper_95"]


@pytest.mark.parametrize("field,bad", [("bootstrap_repetitions", -1), ("bootstrap_repetitions", 257), ("bootstrap_repetitions", 1), ("bootstrap_repetitions", math.nan), ("bootstrap_repetitions", True), ("bootstrap_block_weeks", 1), ("bootstrap_block_weeks", 53), ("bootstrap_block_weeks", math.inf), ("bootstrap_seed", math.nan), ("bootstrap_seed", True)])
def test_invalid_bootstrap_settings_are_not_silently_replaced(field, bad):
    result = evidence.evaluate_risk_evidence([(asset(), .5)], **{field: bad})
    assert result["bootstrap"]["status"] == "invalid"
    json.dumps(result, allow_nan=False)


def test_multiple_invalid_settings_never_echo_nonfinite_json_including_cash_only():
    for rows in ([], [(asset(), .5)], [(asset([]), .5)]):
        result = evidence.evaluate_risk_evidence(rows, bootstrap_repetitions=math.nan,
                                                bootstrap_block_weeks=math.inf, bootstrap_seed=math.nan)
        assert result["status"] == "invalid"
        json.dumps(result, allow_nan=False)
        result = evidence.evaluate_risk_evidence(rows, horizon_weeks=math.nan)
        assert result["status"] == "invalid"
        json.dumps(result, allow_nan=False)


def test_unknown_exposures_are_not_manufactured_from_listing_currency_or_name():
    item = asset(sleeve="aggregate_bond")
    item.update({"currency": "EUR", "name": "EUR Hedged Government Fund", "modified_duration": 0})
    result = evidence.evaluate_risk_evidence([(item, .5)], as_of="2026-10-06", bootstrap_repetitions=0)
    scenarios = {row["id"]: row for row in result["stress"]["scenarios"]}
    for scenario in ("rates_up_200bps", "credit_spreads_up_300bps", "unhedged_currency_down_20pct"):
        assert scenarios[scenario]["status"] == "unknown"
        assert scenarios[scenario]["portfolio_loss"] is None


def test_verified_factor_scenarios_are_explicit_simple_sensitivities():
    item = asset(sleeve="aggregate_bond", exposure={"evidence_verified": True, "as_of_date": "2026-10-01", "modified_duration": 7, "credit_spread_duration": 4, "unhedged_currency_fraction": .3})
    result = evidence.evaluate_risk_evidence([(item, .6)], as_of="2026-10-06", max_drawdown_loss=.2, bootstrap_repetitions=0)
    scenarios = {row["id"]: row for row in result["stress"]["scenarios"]}
    assert scenarios["rates_up_200bps"]["portfolio_loss"] == pytest.approx(.6 * 7 * .02)
    assert scenarios["credit_spreads_up_300bps"]["portfolio_loss"] == pytest.approx(.6 * 4 * .03)
    assert scenarios["unhedged_currency_down_20pct"]["portfolio_loss"] == pytest.approx(.6 * .3 * .2)
    assert result["stress"]["calibrated"] is False
    assert result["stress"]["reverse_stress"]["each_invested_asset_loss_at_limit"] == pytest.approx(1 / 3)


@pytest.mark.parametrize("change", [{"evidence_verified": False}, {"evidence_verified": "true"}, {"as_of_date": "2026-10-07"}, {"as_of_date": "2026-02-30"}, {"as_of_date": None}, {"modified_duration": math.nan}, {"modified_duration": True}, {"modified_duration": 100}])
def test_unverified_future_corrupt_or_out_of_range_exposure_stays_unknown(change):
    metadata = {"evidence_verified": True, "as_of_date": "2026-10-01", "modified_duration": 7}
    metadata.update(change)
    result = evidence.evaluate_risk_evidence([(asset(sleeve="government_bond", exposure=metadata), .5)], as_of="2026-10-06", bootstrap_repetitions=0)
    scenario = next(row for row in result["stress"]["scenarios"] if row["id"] == "rates_up_200bps")
    assert scenario["status"] == "unknown"
    assert scenario["portfolio_loss"] is None
    json.dumps(result, allow_nan=False)


def test_fixed_quantity_stress_paths_keep_cash_and_full_recovery_consistent():
    result = evidence.evaluate_risk_evidence([(asset(), .4)], bootstrap_repetitions=0)
    scenarios = {row["id"]: row for row in result["stress"]["scenarios"]}
    assert scenarios["sustained_decline"]["portfolio_loss"] == pytest.approx(.4 * (1 - .99 ** 52))
    assert scenarios["decline_then_full_rebound"]["portfolio_loss"] == 0
    assert scenarios["decline_then_full_rebound"]["max_drawdown_loss"] == .1


def test_historical_horizon_windows_are_contiguous_and_explicitly_overlapping():
    result = evidence.evaluate_risk_evidence([(asset([.01] * 104), .5)], horizon_weeks=52, goal_return=.3, bootstrap_repetitions=0)
    horizon = result["horizon"]
    assert horizon["status"] == "observed"
    assert horizon["historical_windows"] == 53
    assert horizon["min_horizon_return"] == pytest.approx(1.005 ** 52 - 1)
    assert horizon["historical_goal_shortfall_count"] == 53
    assert horizon["overlapping_windows_are_not_independent"]
    assert "nominal_not_inflation_adjusted" in horizon["assumptions"]


@pytest.mark.parametrize("horizon", [0, -1, 5201, 1.5, True, math.inf, "52"])
def test_horizon_is_explicit_integer_weeks_and_invalid_values_are_not_guessed(horizon):
    result = evidence.evaluate_risk_evidence([(asset(), .5)], horizon_weeks=horizon, bootstrap_repetitions=0)
    assert result["horizon"]["status"] == "invalid"
    json.dumps(result, allow_nan=False)


def test_goal_requires_an_explicit_horizon_and_long_horizon_does_not_create_data():
    result = evidence.evaluate_risk_evidence([(asset(), .5)], goal_return=.2, bootstrap_repetitions=0)
    assert result["horizon"]["reason"] == "goal_requires_explicit_horizon"
    result = evidence.evaluate_risk_evidence([(asset(), .5)], horizon_weeks=5200, bootstrap_repetitions=0)
    assert result["horizon"]["status"] == "unavailable"
    assert result["horizon"]["historical_windows"] == 0


@pytest.mark.parametrize("goal", [True, math.nan, math.inf, -1.1, "0.2"])
def test_invalid_goal_return_is_reported_without_probability_output(goal):
    result = evidence.evaluate_risk_evidence([(asset(), .5)], horizon_weeks=52, goal_return=goal, bootstrap_repetitions=0)
    assert result["horizon"]["status"] == "invalid"
    assert "historical_goal_shortfall_fraction" not in result["horizon"]
    json.dumps(result, allow_nan=False)
