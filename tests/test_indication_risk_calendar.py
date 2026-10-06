"""Trading-session dates, rather than UTC bar labels, align weekly risk."""

import sys
from datetime import UTC, date, datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


COMPONENT = Path(__file__).resolve().parents[1] / "custom_components/investment"


def load(name, filename):
    spec = spec_from_file_location(name, COMPONENT / filename)
    module = module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


model = load("risk_calendar_model", "validated_model.py")
models = load("risk_calendar_models", "models.py")


def point(session, value, *, timezone="Europe/Berlin", hour=0):
    day = date.fromisoformat(session)
    timestamp = datetime.combine(day, datetime.min.time(), ZoneInfo(timezone)).replace(hour=hour).timestamp()
    return models.HistoryPoint(timestamp, value, session_date=session)


def strict(points, **kwargs):
    return model.weekly_return_map_from_points(points, require_session_dates=True, **kwargs)


def test_generic_history_serialization_retains_legacy_two_field_shape():
    original = models.HistoryPoint(1234567890, 100.0)
    assert original.session_date is None
    assert original.as_dict() == {"ts": 1234567890, "value": 100.0}
    assert models.HistoryPoint(**original.as_dict()) == original


def test_provider_session_date_survives_cache_serialization_without_relabelling_raw_time():
    original = point("2026-09-21", 100)
    payload = original.as_dict()
    assert payload["session_date"] == "2026-09-21"
    assert datetime.fromtimestamp(payload["ts"], UTC).isoformat() == "2026-09-20T22:00:00+00:00"
    assert models.HistoryPoint(**payload) == original


def test_xetra_monday_session_is_not_shifted_to_previous_week_by_sunday_utc_label():
    points = [point("2026-09-14", 100), point("2026-09-21", 110)]
    assert strict(points) == {"2026-W39": pytest.approx(.1)}
    legacy_points = [(entry.ts, entry.value) for entry in points]
    assert model.weekly_return_map_from_points(legacy_points) == {"2026-W38": pytest.approx(.1)}


def test_europe_and_new_york_prices_align_to_the_same_economic_week():
    europe = [point("2026-09-14", 100), point("2026-09-21", 110)]
    america = [point("2026-09-14", 200, timezone="America/New_York"),
               point("2026-09-21", 220, timezone="America/New_York")]
    assert strict(europe) == strict(america) == {"2026-W39": pytest.approx(.1)}
    assert {datetime.fromtimestamp(entry.ts, UTC).weekday() for entry in europe} == {6}
    assert {datetime.fromtimestamp(entry.ts, UTC).weekday() for entry in america} == {0}


@pytest.mark.parametrize("timezone", ["Asia/Tokyo", "Pacific/Auckland", "Pacific/Kiritimati"])
def test_positive_offset_session_dates_do_not_use_previous_utc_week(timezone):
    points = [point("2026-09-28", 100, timezone=timezone),
              point("2026-10-05", 105, timezone=timezone)]
    assert datetime.fromtimestamp(points[-1].ts, UTC).date().isoformat() == "2026-10-04"
    assert strict(points) == {"2026-W41": pytest.approx(.05)}


def test_negative_offset_sunday_session_is_not_moved_to_monday_utc_week():
    points = [point("2025-12-28", 100, timezone="America/New_York", hour=23),
              point("2026-01-04", 110, timezone="America/New_York", hour=23)]
    assert datetime.fromtimestamp(points[-1].ts, UTC).date().isoformat() == "2026-01-05"
    assert strict(points) == {"2026-W01": pytest.approx(.1)}


@pytest.mark.parametrize("sessions,timezone,expected_week,hours", [
    (("2026-03-23", "2026-03-30"), "Europe/Berlin", "2026-W14", 167),
    (("2026-10-19", "2026-10-26"), "Europe/Berlin", "2026-W44", 169),
    (("2026-03-02", "2026-03-09"), "America/New_York", "2026-W11", 167),
    (("2026-10-26", "2026-11-02"), "America/New_York", "2026-W45", 169),
])
def test_dst_changes_do_not_invent_a_missing_or_extra_risk_week(sessions, timezone, expected_week, hours):
    points = [point(sessions[0], 100, timezone=timezone), point(sessions[1], 110, timezone=timezone)]
    assert (points[1].ts - points[0].ts) / 3600 == hours
    assert strict(points) == {expected_week: pytest.approx(.1)}


@pytest.mark.parametrize("sessions,expected_week", [
    (("2015-12-28", "2016-01-04"), "2016-W01"),
    (("2020-12-28", "2021-01-04"), "2021-W01"),
    (("2025-12-22", "2025-12-29"), "2026-W01"),
])
def test_iso_year_transition_uses_session_calendar(sessions, expected_week):
    assert strict([point(sessions[0], 100), point(sessions[1], 110)]) == {
        expected_week: pytest.approx(.1),
    }


def test_real_missing_weeks_are_not_synthesized_by_calendar_normalization():
    points = [point("2026-09-14", 100), point("2026-09-21", 110),
              point("2026-10-05", 121), point("2026-10-12", 133.1)]
    assert strict(points) == {
        "2026-W39": pytest.approx(.1),
        "2026-W42": pytest.approx(.1),
    }


def test_last_daily_close_is_selected_by_raw_timestamp_inside_each_session_week():
    points = [point("2026-10-02", 120, hour=17), point("2026-09-25", 100, hour=17),
              point("2026-10-02", 110, hour=9), point("2026-09-28", 105, hour=17)]
    assert strict(points) == {"2026-W40": pytest.approx(.2)}


def test_raw_timestamp_remains_authoritative_for_rolling_lookback():
    points = [point("2026-10-19", 100), point("2026-10-26", 110)]
    # The autumn change makes seven calendar dates span169 actual hours.
    # Calendar normalization must not silently lengthen the requested window.
    assert strict(points, lookback_days=7) == {}
    assert strict(points, lookback_days=8) == {"2026-W44": pytest.approx(.1)}


@pytest.mark.parametrize("kind", ["object", "mapping", "tuple"])
def test_all_supported_point_forms_preserve_session_date(kind):
    points = [point("2026-09-14", 100), point("2026-09-21", 110)]
    if kind == "mapping":
        points = [entry.as_dict() for entry in points]
    elif kind == "tuple":
        points = [(entry.ts, entry.value, entry.session_date) for entry in points]
    assert strict(points) == {"2026-W39": pytest.approx(.1)}


@pytest.mark.parametrize("kind", ["object", "mapping", "tuple"])
def test_strict_mode_cannot_infer_a_missing_session_date_from_utc(kind):
    first, last = point("2026-09-14", 100), point("2026-09-21", 110)
    if kind == "object":
        last = models.HistoryPoint(last.ts, last.value)
    elif kind == "mapping":
        last = {"ts": last.ts, "value": last.value}
    else:
        last = (last.ts, last.value)
    assert strict([first, last]) == {}


@pytest.mark.parametrize("bad", [
    "", "2026-02-29", "2026-09-31", "20260921", "2026-W39-1", " 2026-09-21",
    "2026-09-21 ", "2026-09-21T00:00:00+02:00", True, False, 20260921,
    date(2026, 9, 21), [], {},
])
@pytest.mark.parametrize("strict_mode", [False, True])
def test_explicit_invalid_session_date_never_silently_falls_back_to_utc(bad, strict_mode):
    points = [point("2026-09-14", 100).as_dict(), point("2026-09-21", 110).as_dict()]
    points[-1]["session_date"] = bad
    assert model.weekly_return_map_from_points(points, require_session_dates=strict_mode) == {}


@pytest.mark.parametrize("bad_date", ["2001-09-21", "2026-09-18", "2026-09-23"])
def test_an_arbitrary_valid_iso_date_cannot_relabel_distant_source_time(bad_date):
    points = [point("2026-09-14", 100).as_dict(), point("2026-09-21", 110).as_dict()]
    points[-1]["session_date"] = bad_date
    assert strict(points) == {}


def test_missing_calendar_proof_is_rejected_before_old_rows_can_be_discarded():
    stale = models.HistoryPoint(datetime(1990, 1, 1, tzinfo=UTC).timestamp(), 50)
    recent = [point("2026-09-14", 100), point("2026-09-21", 110)]
    assert strict([stale, *recent]) == {}
    assert model.weekly_return_map_from_points([stale, *recent]) == {"2026-W39": pytest.approx(.1)}


@pytest.mark.parametrize("field", ["ts", "value"])
@pytest.mark.parametrize("boolean", [False, True])
def test_strict_mode_rejects_raw_booleans_before_numeric_coercion(field, boolean):
    points = [point("1970-01-01", 100, timezone="UTC", hour=1).as_dict(),
              point("1970-01-08", 110, timezone="UTC").as_dict()]
    points[0][field] = boolean
    assert strict(points) == {}


def test_boolean_boundary_hardening_does_not_change_legacy_numeric_coercion():
    last = datetime(1970, 1, 8, tzinfo=UTC).timestamp()
    assert model.weekly_return_map_from_points([(True, 100), (last, 110)]) == {
        "1970-W02": pytest.approx(.1),
    }
    first = datetime(2026, 9, 14, tzinfo=UTC).timestamp()
    last = datetime(2026, 9, 21, tzinfo=UTC).timestamp()
    assert model.weekly_return_map_from_points([(first, True), (last, 2)]) == {
        "2026-W39": 1.0,
    }


def test_legacy_offline_input_keeps_original_utc_behavior():
    points = [(datetime(2020, 12, 28, tzinfo=UTC).timestamp(), 10),
              (datetime(2021, 1, 4, tzinfo=UTC).timestamp(), 11),
              (datetime(2021, 1, 18, tzinfo=UTC).timestamp(), 12),
              (datetime(2021, 1, 25, tzinfo=UTC).timestamp(), 13)]
    expected = {"2021-W01": pytest.approx(.1), "2021-W04": pytest.approx(13 / 12 - 1)}
    assert model.weekly_return_map_from_points(points) == expected
    assert model.weekly_return_map_from_points(points, require_session_dates=False) == expected
    assert strict(points) == {}


@pytest.mark.parametrize("price_count,expected", [(52, False), (53, True)])
def test_calendar_alignment_keeps_exact_52_weekly_return_requirement(price_count, expected):
    first = date(2025, 1, 6)
    points = [point((first + timedelta(weeks=index)).isoformat(), 100 + index * .01)
              for index in range(price_count)]
    returns = strict(points)
    assert len(returns) == price_count - 1
    signature = model.risk_signature([({"risk_weekly_returns": returns}, .2)])
    assert model.within_risk_target(signature, "medium") is expected
