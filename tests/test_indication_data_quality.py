"""Source-time policy boundaries and realistic passing indication histories."""

import json
import math
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest


COMPONENT = Path(__file__).resolve().parents[1] / "custom_components/investment"


def load_module(name, filename):
    spec = spec_from_file_location(name, COMPONENT / filename)
    module = module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


quality = load_module("indication_data_quality_subject", "data_quality.py")
validated = load_module("indication_data_quality_model", "validated_model.py")
NOW = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)


def quote(stamp, *, now=NOW, category="stock"):
    return quality.assess_quote_freshness(stamp, analysis_time=now, category=category)


def series(*, latest=NOW - timedelta(hours=1), count=53, step=timedelta(weeks=1)):
    return [
        (int((latest - (count - 1 - index) * step).timestamp()), 100 + 0.01 * index)
        for index in range(count)
    ]


def history(points, *, now=NOW, category="stock", cadence="weekly", role="risk"):
    return quality.assess_history_freshness(
        points, analysis_time=now, category=category, cadence=cadence, role=role
    )


def test_fresh_quote_has_exact_source_time_and_fixed_analysis_evidence():
    stamp = NOW - timedelta(minutes=17)
    result = quote(stamp.timestamp())
    assert result["eligible"] is True
    assert result["reasons"] == []
    assert result["source_as_of_ts"] == int(stamp.timestamp())
    assert result["analysis_time_ts"] == int(NOW.timestamp())
    assert result["source_age_seconds"] == 17 * 60
    assert result["policy_id"] == "indication_source_time_v1"
    assert json.loads(json.dumps(result, allow_nan=False)) == result


@pytest.mark.parametrize("stamp", [None, False, True, "", "2026-10-06", str(NOW.timestamp()), 0, -1, math.nan, math.inf, -math.inf, 10**1000, NOW.replace(tzinfo=None)])
def test_missing_or_corrupt_quote_timestamp_never_becomes_fresh(stamp):
    result = quote(stamp)
    assert result["eligible"] is False
    assert result["source_as_of_ts"] is None
    assert result["source_age_seconds"] is None
    assert result["reasons"] == [
        "quote_timestamp_missing" if stamp is None else "quote_timestamp_invalid"
    ]


@pytest.mark.parametrize("analysis_time", [None, True, 0, math.nan, math.inf, "2026-10-06T18:00:00Z", NOW.replace(tzinfo=None)])
def test_bad_analysis_time_fails_both_gates_instead_of_using_wall_clock(analysis_time):
    for result in (
        quote(NOW, now=analysis_time),
        history(series(), now=analysis_time),
    ):
        assert result["eligible"] is False
        assert "analysis_time_invalid" in result["reasons"]
        assert result["analysis_time_ts"] is None


def test_aware_datetimes_normalize_to_utc_without_local_timezone_assumptions():
    source = NOW - timedelta(hours=1)
    berlin = timezone(timedelta(hours=2))
    offset = timezone(timedelta(hours=-7))
    expected = quote(source)
    assert quote(source.astimezone(berlin), now=NOW.astimezone(offset)) == expected
    assert quote(source.timestamp(), now=NOW.timestamp()) == expected


@pytest.mark.parametrize("ahead", [0.001, 1, 300, 86400])
def test_any_future_quote_is_rejected_before_timestamp_rounding(ahead):
    result = quote(NOW.timestamp() + ahead)
    assert result["eligible"] is False
    assert result["reasons"] == ["quote_timestamp_future"]
    assert result["source_age_seconds"] == pytest.approx(-ahead, abs=1e-6)


@pytest.mark.parametrize("now", [datetime(2026, 10, 3, 23, 0, tzinfo=UTC), datetime(2026, 10, 4, 23, 0, tzinfo=UTC), datetime(2026, 10, 5, 8, 0, tzinfo=UTC)])
@pytest.mark.parametrize("category", ["stock", "etf", "fund", "bond", "fx"])
def test_friday_close_stays_usable_across_normal_weekend_and_monday_open(now, category):
    friday_close = datetime(2026, 10, 2, 20, 0, tzinfo=UTC)
    assert quote(friday_close, now=now, category=category)["eligible"] is True


def test_exchange_quote_weekday_limit_is_inclusive_with_weekend_excluded():
    # Friday noon -> Wednesday noon is exactly 72 elapsed weekday hours.
    source = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    boundary = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    assert quote(source, now=boundary)["eligible"] is True
    stale = quote(source, now=boundary + timedelta(microseconds=1))
    assert stale["eligible"] is False
    assert stale["reasons"] == ["quote_stale"]


def test_multiweek_quote_cannot_be_revalidated_by_a_recent_fetch():
    source = NOW - timedelta(weeks=3)
    fetched_now = {"market_time": source.timestamp(), "fetched_at": NOW.timestamp()}
    result = quote(fetched_now["market_time"])
    assert result["eligible"] is False
    assert result["source_as_of_ts"] == int(source.timestamp())
    assert "quote_stale" in result["reasons"]


def test_crypto_quote_limit_is_calendar_based_even_over_a_weekend():
    saturday = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    boundary = saturday + timedelta(hours=24)
    assert quote(saturday, now=boundary, category="crypto")["eligible"] is True
    assert quote(saturday, now=boundary + timedelta(seconds=1), category="crypto")["reasons"] == ["quote_stale"]
    assert quote(saturday, now=boundary + timedelta(seconds=1), category="stock")["eligible"] is True


@pytest.mark.parametrize("representation", ["tuple", "mapping", "object"])
def test_fresh_three_year_weekly_history_accepts_all_supported_point_types(representation):
    points = series(count=157)
    if representation == "mapping":
        points = [{"ts": ts, "value": price} for ts, price in points]
    elif representation == "object":
        points = [SimpleNamespace(ts=ts, value=price) for ts, price in points]
    original = deepcopy(points)
    result = history(points)
    assert result["eligible"] is True
    assert result["reasons"] == []
    assert result["observations"] == 157
    assert result["distinct_observations"] == 157
    assert result["latest_interval_seconds"] == 7 * 86400
    assert result["source_as_of_ts"] == int((NOW - timedelta(hours=1)).timestamp())
    assert points == original
    assert json.loads(json.dumps(result, allow_nan=False)) == result


def test_history_accepts_aware_datetime_points_and_an_iterable():
    points = [(datetime.fromtimestamp(ts, UTC), value) for ts, value in series()]
    assert history(iter(points))["eligible"] is True


@pytest.mark.parametrize("role", ["signal", "risk", "fx"])
def test_weekly_period_start_label_is_valid_through_following_weekend(role):
    # A last completed week labelled Monday is nearly 14 days old on Sunday.
    latest = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
    sunday = datetime(2026, 10, 4, 23, 59, tzinfo=UTC)
    points = series(latest=latest)
    result = history(points, now=sunday, role=role)
    assert result["eligible"] is True
    assert result["max_calendar_age_seconds"] == 14 * 86400


@pytest.mark.parametrize("category", ["stock", "crypto", "fx"])
def test_weekly_history_age_policy_has_an_explicit_inclusive_boundary(category):
    points = series(latest=NOW - timedelta(days=14))
    assert history(points, category=category)["eligible"] is True
    result = history(points, now=NOW + timedelta(seconds=1), category=category)
    assert result["eligible"] is False
    assert result["reasons"] == ["risk_history_stale"]


def test_crypto_daily_history_does_not_inherit_exchange_weekend_grace():
    sunday = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    points = series(latest=sunday - timedelta(hours=48), step=timedelta(days=1))
    assert history(points, now=sunday, category="crypto", cadence="daily")["eligible"] is True
    stale = history(points, now=sunday + timedelta(seconds=1), category="crypto", cadence="daily")
    assert stale["eligible"] is False
    assert stale["reasons"] == ["risk_history_stale"]


@pytest.mark.parametrize("role", ["signal", "risk", "fx"])
def test_exchange_daily_history_allows_friday_observation_on_monday(role):
    friday = datetime(2026, 10, 2, 0, 0, tzinfo=UTC)
    monday = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
    points = series(latest=friday, step=timedelta(days=1))
    assert history(points, now=monday, cadence="daily", role=role)["eligible"] is True


@pytest.mark.parametrize("role", ["signal", "risk", "fx"])
def test_old_raw_feed_stays_stale_even_with_complete_low_volatility_history(role):
    points = series(latest=NOW - timedelta(days=90), count=157)
    result = history(points, role=role)
    assert result["eligible"] is False
    assert result["reasons"] == [f"{role}_history_stale"]
    assert result["distinct_observations"] == 157


@pytest.mark.parametrize("bad_ts", [None, False, True, "", "2026-10-06", 0, -2, math.nan, math.inf, 10**1000, NOW.replace(tzinfo=None)])
def test_one_bad_historical_timestamp_is_not_silently_dropped(bad_ts):
    points = series(count=157)
    points[30] = (bad_ts, 100.0)
    result = history(points)
    assert result["eligible"] is False
    reason = "risk_history_timestamp_missing" if bad_ts is None else "risk_history_timestamp_invalid"
    assert reason in result["reasons"]
    assert result["source_as_of_ts"] == points[-1][0]


@pytest.mark.parametrize("bad_price", [None, False, True, "100.0", "bad", 0, -1, math.nan, math.inf, -math.inf, 10**1000])
def test_one_corrupt_historical_price_is_not_silently_dropped(bad_price):
    points = series(count=157)
    points[30] = (points[30][0], bad_price)
    result = history(points)
    assert result["eligible"] is False
    assert result["reasons"] == ["risk_history_value_invalid"]


@pytest.mark.parametrize("position", [0, 30, -1])
def test_future_history_anywhere_in_the_feed_blocks_it(position):
    points = series(count=157)
    points[position] = (NOW.timestamp() + 0.125, points[position][1])
    result = history(points)
    assert result["eligible"] is False
    assert "risk_history_timestamp_future" in result["reasons"]


@pytest.mark.parametrize("conflicting", [False, True])
def test_duplicate_source_observations_never_count_as_extra_evidence(conflicting):
    points = series()
    ts, value = points[-1]
    points.append((ts, value + 1 if conflicting else value))
    result = history(points)
    assert result["eligible"] is False
    assert result["reasons"] == ["risk_history_duplicate_timestamp"]
    assert result["observations"] == 54
    assert result["distinct_observations"] == 53


def test_out_of_order_feed_cannot_pass_then_reach_scorer_in_wrong_order():
    points = series()
    points[1], points[2] = points[2], points[1]
    result = history(points)
    assert result["eligible"] is False
    assert result["reasons"] == ["risk_history_not_chronological"]


@pytest.mark.parametrize("role", ["signal", "risk", "fx"])
def test_single_current_observation_cannot_rejuvenate_an_old_series(role):
    points = series(latest=NOW - timedelta(days=90), count=157)
    points.append((NOW.timestamp(), 102.0))
    result = history(points, role=role)
    assert result["eligible"] is False
    assert result["reasons"] == [f"{role}_history_tail_gap"]
    assert result["source_age_seconds"] == 0


@pytest.mark.parametrize("bad", [None, {}, "bad", 123, b"bad"])
def test_bad_history_container_returns_evidence_without_raising(bad):
    result = history(bad)
    assert result["eligible"] is False
    assert result["reasons"] == ["risk_history_invalid_container"]


@pytest.mark.parametrize("points,reason", [([], "risk_history_empty"), ([(NOW.timestamp(), 100)], "risk_history_insufficient_observations")])
def test_empty_or_single_observation_feed_is_not_enough(points, reason):
    result = history(points)
    assert result["eligible"] is False
    assert result["reasons"] == [reason]


def test_unknown_cadence_is_not_assumed_daily_or_weekly():
    result = history(series(), cadence="monthly")
    assert result["eligible"] is False
    assert result["reasons"] == ["risk_history_cadence_unsupported"]


def test_unknown_evidence_role_fails_closed():
    result = history(series(), role="fetched_at")
    assert result["eligible"] is False
    assert result["reasons"] == ["history_role_unsupported"]


@pytest.mark.parametrize("price_count,expected", [(52, False), (53, True)])
def test_freshness_preserves_exact_downstream_52_weekly_return_requirement(price_count, expected):
    points = series(count=price_count)
    assert history(points)["eligible"] is True
    returns = validated.weekly_return_map_from_points(points)
    assert len(returns) == price_count - 1
    signature = validated.risk_signature([({"risk_weekly_returns": returns}, 0.2)])
    assert validated.within_risk_target(signature, "medium") is expected


def test_fresh_fx_and_recent_prices_pass_independently_but_old_fx_fails():
    instrument = series()
    recent_fx = [(ts, 0.9 + index * 0.0001) for index, (ts, _) in enumerate(series())]
    assert history(instrument)["eligible"] is True
    assert history(recent_fx, category="fx", role="fx")["eligible"] is True
    old_fx = [(ts - 90 * 86400, rate) for ts, rate in recent_fx]
    result = history(old_fx, category="fx", role="fx")
    assert result["eligible"] is False
    assert result["reasons"] == ["fx_history_stale"]


def test_advancing_analysis_time_can_never_refresh_unchanged_stale_feed():
    points = series(latest=NOW - timedelta(days=15))
    for days in (0, 1, 2, 7, 31, 365):
        result = history(points, now=NOW + timedelta(days=days))
        assert result["eligible"] is False
        assert "risk_history_stale" in result["reasons"]


def test_quote_and_history_need_their_own_fresh_source_times():
    assert quote(NOW)["eligible"] is True
    assert history(series(latest=NOW - timedelta(days=60)))["eligible"] is False
    assert quote(NOW - timedelta(days=60))["eligible"] is False
    assert history(series())["eligible"] is True


def convert(points, fx_points, *, now=NOW, cadence="daily", scale=1.0):
    return quality.convert_history_with_observed_fx(
        points, fx_points, analysis_time=now, cadence=cadence, scale=scale
    )


def at(day, hour=0):
    return datetime(2026, 10, day, hour, tzinfo=UTC).timestamp()


def test_fx_conversion_uses_only_observations_known_at_each_asset_timestamp():
    points = [(at(1, 12), 100), (at(2, 12), 110), (at(6, 12), 120)]
    fx = [(at(1), .8), (at(2), .82), (at(5), .83), (at(6), .84)]
    original = deepcopy((points, fx))
    converted, evidence = convert(points, fx)
    assert [stamp for stamp, _ in converted] == [stamp for stamp, _ in points]
    assert [value for _, value in converted] == pytest.approx([80, 90.2, 100.8])
    assert evidence["eligible"] is True
    assert evidence["reasons"] == []
    assert evidence["converted_observations"] == 3
    assert evidence["dropped_leading_observations"] == 0
    assert evidence["source_as_of_ts"] == at(6)
    assert evidence["asset_source_as_of_ts"] == at(6, 12)
    assert evidence["fx_evidence"]["role"] == "fx"
    assert (points, fx) == original
    assert json.loads(json.dumps(evidence, allow_nan=False)) == evidence


def test_fx_conversion_applies_currency_unit_scale_exactly_once():
    points = [(at(5, 12), 12345), (at(6, 12), 13000)]
    fx = [(at(5), 1.2), (at(6), 1.3)]
    converted, evidence = convert(points, fx, scale=.01)
    assert evidence["eligible"] is True
    assert [value for _, value in converted] == pytest.approx([148.14, 169.0])


@pytest.mark.parametrize("points,reason", [([], "fx_history_empty"), (None, "fx_history_invalid_container"), ({}, "fx_history_invalid_container"), ([(at(6), .9)], "fx_history_insufficient_observations")])
def test_empty_or_missing_fx_never_becomes_identity_rate_one(points, reason):
    converted, evidence = convert([(at(5, 12), 100), (at(6, 12), 120)], points)
    assert converted == []
    assert evidence["eligible"] is False
    assert reason in evidence["reasons"]
    assert evidence["converted_observations"] == 0


def test_future_fx_observation_fails_entire_conversion():
    fx = [(at(5), .9), (at(6), .91), (NOW.timestamp() + .1, .92)]
    converted, evidence = convert([(at(5, 12), 100), (at(6, 12), 120)], fx)
    assert converted == []
    assert evidence["eligible"] is False
    assert "fx_history_timestamp_future" in evidence["reasons"]


def test_stale_fx_tail_is_not_relabelled_with_current_asset_dates():
    fx = [(at(1) - 30 * 86400, .9), (at(2) - 30 * 86400, .91)]
    assets = [(at(5, 12), 100), (at(6, 12), 120)]
    converted, evidence = convert(assets, fx)
    assert converted == []
    assert evidence["eligible"] is False
    assert "fx_history_stale" in evidence["reasons"]
    assert evidence["source_as_of_ts"] == fx[-1][0]
    assert evidence["asset_source_as_of_ts"] == assets[-1][0]


def test_stale_interior_fx_gap_rejects_all_partial_output_despite_fresh_tail():
    sep_1 = datetime(2026, 9, 1, tzinfo=UTC).timestamp()
    fx = [(sep_1, .8), (sep_1 + 86400, .81), (at(5), .9), (at(6), .91)]
    assets = [(sep_1 + 86400, 100), (sep_1 + 14 * 86400, 110), (at(6, 12), 120)]
    assert history(fx, category="fx", cadence="daily", role="fx")["eligible"] is True
    converted, evidence = convert(assets, fx)
    assert converted == []
    assert evidence["eligible"] is False
    assert evidence["reasons"] == ["fx_history_asof_gap"]
    assert evidence["uncovered_asset_ts"] == assets[1][0]
    assert evidence["last_observed_fx_ts"] == fx[1][0]
    assert evidence["converted_observations"] == 0


def test_fx_carry_across_a_weekend_is_valid_without_guessing_a_future_monday_rate():
    monday = datetime(2026, 10, 5, 10, tzinfo=UTC)
    fx = [(at(1), .88), (at(2), .9)]
    assets = [(at(2, 20), 100), (at(3, 20), 100), (at(4, 20), 100), (at(5, 8), 100)]
    converted, evidence = convert(assets, fx, now=monday)
    assert evidence["eligible"] is True
    assert [value for _, value in converted] == pytest.approx([90, 90, 90, 90])
    assert evidence["max_asof_gap_seconds"] == 80 * 3600


def test_historical_daily_fx_asof_carry_boundary_is_inclusive():
    tuesday = datetime(2026, 9, 1, 12, tzinfo=UTC).timestamp()
    boundary = datetime(2026, 9, 4, 12, tzinfo=UTC).timestamp()
    fx = [(tuesday, .8), (at(5), .9), (at(6), .91)]
    good, good_evidence = convert([(boundary, 100), (at(6, 12), 100)], fx)
    assert good_evidence["eligible"] is True
    assert good[0][1] == pytest.approx(80)
    bad, bad_evidence = convert([(boundary + .001, 100), (at(6, 12), 100)], fx)
    assert bad == []
    assert bad_evidence["eligible"] is False
    assert bad_evidence["reasons"] == ["fx_history_asof_gap"]


def test_leading_asset_dates_are_explicitly_cropped_instead_of_using_future_fx():
    assets = [(at(1, 12), 100), (at(2, 12), 100), (at(5, 12), 100), (at(6, 12), 100)]
    fx = [(at(5), .9), (at(6), .92)]
    converted, evidence = convert(assets, fx)
    assert evidence["eligible"] is True
    assert evidence["dropped_leading_observations"] == 2
    assert evidence["input_observations"] == 4
    assert evidence["converted_observations"] == 2
    assert converted == [(at(5, 12), 90), (at(6, 12), 92)]


def test_all_asset_dates_before_first_fx_observation_fail_without_backfill():
    assets = [(at(1, 12), 100), (at(2, 12), 100)]
    fx = [(at(5), .9), (at(6), .92)]
    converted, evidence = convert(assets, fx)
    assert converted == []
    assert evidence["eligible"] is False
    assert evidence["reasons"] == ["fx_history_no_asof_coverage"]
    assert evidence["dropped_leading_observations"] == 2
    assert evidence["converted_observations"] == 0


@pytest.mark.parametrize("cadence", ["weekly", "monthly", "", None])
def test_grouped_fx_cannot_claim_raw_daily_observation_semantics(cadence):
    converted, evidence = convert([(at(5, 12), 100), (at(6, 12), 100)], [(at(5), .9), (at(6), .92)], cadence=cadence)
    assert converted == []
    assert evidence["eligible"] is False
    assert evidence["reasons"] == ["fx_history_raw_daily_required"]


@pytest.mark.parametrize("scale", [None, True, False, 0, -1, math.nan, math.inf, "0.01"])
def test_invalid_scale_cannot_produce_usable_converted_history(scale):
    converted, evidence = convert([(at(5, 12), 100), (at(6, 12), 100)], [(at(5), .9), (at(6), .92)], scale=scale)
    assert converted == []
    assert evidence["eligible"] is False
    assert evidence["reasons"] == ["fx_conversion_scale_invalid"]


@pytest.mark.parametrize("point,reason", [((None, 100), "asset_history_timestamp_missing"), ((math.nan, 100), "asset_history_timestamp_invalid"), ((NOW.timestamp() + 1, 100), "asset_history_timestamp_future"), ((at(6, 12), math.nan), "asset_history_value_invalid"), ((at(6, 12), True), "asset_history_value_invalid")])
def test_bad_asset_inputs_block_fx_conversion(point, reason):
    converted, evidence = convert([(at(5, 12), 100), point], [(at(5), .9), (at(6), .92)])
    assert converted == []
    assert evidence["eligible"] is False
    assert reason in evidence["reasons"]


def test_cropping_never_hides_a_corrupt_leading_asset_price():
    assets = [(at(1), math.nan), (at(5, 12), 100), (at(6, 12), 100)]
    converted, evidence = convert(assets, [(at(5), .9), (at(6), .92)])
    assert converted == []
    assert evidence["eligible"] is False
    assert evidence["reasons"] == ["asset_history_value_invalid"]
    assert evidence["dropped_leading_observations"] == 0


@pytest.mark.parametrize("kind", ["asset_order", "asset_duplicate", "fx_order", "fx_duplicate"])
def test_conversion_cannot_reorder_or_count_duplicate_evidence(kind):
    assets = [(at(5, 12), 100), (at(6, 12), 100)]
    fx = [(at(5), .9), (at(6), .92)]
    selected = assets if kind.startswith("asset") else fx
    if kind.endswith("order"):
        selected.reverse()
        suffix = "not_chronological"
    else:
        selected.append(selected[-1])
        suffix = "duplicate_timestamp"
    converted, evidence = convert(assets, fx)
    assert converted == []
    assert evidence["eligible"] is False
    prefix = "asset" if kind.startswith("asset") else "fx"
    assert f"{prefix}_history_{suffix}" in evidence["reasons"]


def test_finite_raw_inputs_that_overflow_on_conversion_are_rejected():
    converted, evidence = convert([(at(5, 12), 1e308), (at(6, 12), 1e308)], [(at(5), 2), (at(6), 2)])
    assert converted == []
    assert evidence["eligible"] is False
    assert evidence["reasons"] == ["fx_converted_value_invalid"]


@pytest.mark.parametrize("representation", ["mapping", "datetime_object", "generator"])
def test_conversion_supports_standard_point_forms(representation):
    assets = [(at(5, 12), 100), (at(6, 12), 100)]
    fx = [(at(5), .9), (at(6), .92)]
    if representation == "mapping":
        assets = [{"ts": ts, "value": value} for ts, value in assets]
        fx = [{"ts": ts, "value": value} for ts, value in fx]
    elif representation == "datetime_object":
        assets = [SimpleNamespace(ts=datetime.fromtimestamp(ts, UTC), value=value) for ts, value in assets]
        fx = [SimpleNamespace(ts=datetime.fromtimestamp(ts, UTC), value=value) for ts, value in fx]
    else:
        assets = iter(assets)
        fx = iter(fx)
    converted, evidence = convert(assets, fx)
    assert evidence["eligible"] is True
    assert converted == [(at(5, 12), 90), (at(6, 12), 92)]


@pytest.mark.parametrize("price_count,expected", [(53, False), (54, True)])
def test_leading_fx_crop_preserves_downstream_52_aligned_return_requirement(price_count, expected):
    assets = series(count=price_count)
    first_fx_ts = assets[1][0]
    fx = [(ts, .9) for ts in range(first_fx_ts, assets[-1][0] + 1, 86400)]
    converted, evidence = convert(assets, fx)
    assert evidence["eligible"] is True
    assert evidence["dropped_leading_observations"] == 1
    assert len(converted) == price_count - 1
    returns = validated.weekly_return_map_from_points(converted)
    assert len(returns) == price_count - 2
    signature = validated.risk_signature([({"risk_weekly_returns": returns}, .2)])
    assert validated.within_risk_target(signature, "medium") is expected


def test_fx_conversion_preserves_source_subseconds_before_and_after_asof_choice():
    assets = [(at(6, 12) + .125, 100), (at(6, 12) + .75, 100)]
    fx = [(at(5), .9), (at(6, 12) + .5, .92)]
    converted, evidence = convert(assets, fx)
    assert evidence["eligible"] is True
    assert converted == [(assets[0][0], 90), (assets[1][0], 92)]
