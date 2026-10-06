"""Deterministic source-time gates for indication inputs.

These thresholds are conservative operational policy, not fitted investment
parameters or a claim of exact exchange-calendar certification. Exchange/FX
daily data get at most 72 elapsed weekday hours, with a seven-calendar-day
ceiling. UTC weekends do not consume weekday hours; exchange holidays and
local market sessions are not modelled. Crypto quotes get 24 calendar hours
and crypto daily history gets 48. Weekly bars get 14 calendar days because
providers may label a completed week by its start or its end.

Only an actual source timestamp can establish recency. A download time, cache
insertion time, or another feed's timestamp must never replace it. Callers pass
one fixed, timezone-aware analysis time sampled after their source fetches.
All timestamps later than that cutoff fail, without rounding or clock-skew
allowances. Returned ``*_ts`` values are integer seconds for JSON output;
comparisons retain source subsecond precision.

This gate validates each raw history before signal calculation or FX
conversion. It does not establish corporate-action adjustment, identity, or
portfolio-risk sufficiency; the separate 52 aligned weekly-return minimum
remains mandatory. A recent tail cannot repair corrupt points, and a lone new
point cannot make a long-stale preceding series fresh.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

POLICY_ID = "indication_source_time_v1"
DAY_SECONDS = 86_400
CRYPTO_QUOTE_MAX_AGE_SECONDS = DAY_SECONDS
CRYPTO_DAILY_MAX_AGE_SECONDS = 2 * DAY_SECONDS
EXCHANGE_MAX_WEEKDAY_AGE_SECONDS = 3 * DAY_SECONDS
EXCHANGE_MAX_CALENDAR_AGE_SECONDS = 7 * DAY_SECONDS
WEEKLY_MAX_AGE_SECONDS = 14 * DAY_SECONDS


def _timestamp(value: Any) -> float | None:
    """Accept real epoch seconds or an aware datetime; never infer units/zones."""
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                return None
            stamp = value.timestamp()
        elif isinstance(value, (int, float)):
            stamp = float(value)
        else:
            return None
        if not math.isfinite(stamp) or stamp <= 0:
            return None
        # Also reject numbers outside datetime's supported epoch range. This
        # catches malformed values before any calendar calculation below.
        datetime.fromtimestamp(stamp, UTC)
    except (OverflowError, OSError, TypeError, ValueError):
        return None
    return stamp


def _positive_value(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _weekday_seconds(start_ts: float, end_ts: float) -> float:
    """Integrate elapsed UTC weekday time, without counting weekend closure."""
    start = datetime.fromtimestamp(start_ts, UTC)
    end = datetime.fromtimestamp(end_ts, UTC)
    days = (end.date() - start.date()).days
    weeks, remainder = divmod(days, 7)
    weekday_days = weeks * 5 + sum(
        (start.weekday() + offset) % 7 < 5 for offset in range(remainder)
    )
    elapsed = float(weekday_days * DAY_SECONDS)
    if start.weekday() < 5:
        elapsed -= (start - start.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds()
    if end.weekday() < 5:
        elapsed += (end - end.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds()
    return max(0.0, elapsed)


def _limits(category: str, cadence: str, *, quote: bool = False) -> tuple[int, int | None]:
    if cadence == "weekly":
        return WEEKLY_MAX_AGE_SECONDS, None
    if str(category or "").strip().lower() == "crypto":
        return (CRYPTO_QUOTE_MAX_AGE_SECONDS if quote else CRYPTO_DAILY_MAX_AGE_SECONDS), None
    return EXCHANGE_MAX_CALENDAR_AGE_SECONDS, EXCHANGE_MAX_WEEKDAY_AGE_SECONDS


def _age_exceeds_policy(
    start_ts: float, end_ts: float, calendar_limit: int, weekday_limit: int | None
) -> bool:
    if end_ts - start_ts > calendar_limit:
        return True
    return weekday_limit is not None and _weekday_seconds(start_ts, end_ts) > weekday_limit


def _assessment(analysis_time: Any) -> tuple[dict[str, Any], float | None]:
    analysis_ts = _timestamp(analysis_time)
    return {
        "eligible": False,
        "reasons": [] if analysis_ts is not None else ["analysis_time_invalid"],
        "policy_id": POLICY_ID,
        "analysis_time_ts": int(analysis_ts) if analysis_ts is not None else None,
        "source_as_of_ts": None,
        "source_age_seconds": None,
    }, analysis_ts


def _finish(result: dict[str, Any]) -> dict[str, Any]:
    result["reasons"] = list(dict.fromkeys(result["reasons"]))
    result["eligible"] = not result["reasons"]
    return result


def assess_quote_freshness(
    market_time: Any, *, analysis_time: Any, category: str
) -> dict[str, Any]:
    """Assess the timestamp belonging to the quoted price, never its fetch time."""
    result, analysis_ts = _assessment(analysis_time)
    calendar_limit, weekday_limit = _limits(category, "daily", quote=True)
    result.update({
        "role": "quote",
        "max_calendar_age_seconds": calendar_limit,
        "max_weekday_age_seconds": weekday_limit,
    })
    source_ts = _timestamp(market_time)
    if source_ts is None:
        result["reasons"].append(
            "quote_timestamp_missing" if market_time is None else "quote_timestamp_invalid"
        )
    else:
        result["source_as_of_ts"] = int(source_ts)
        if analysis_ts is not None:
            result["source_age_seconds"] = analysis_ts - source_ts
            if source_ts > analysis_ts:
                result["reasons"].append("quote_timestamp_future")
            elif _age_exceeds_policy(source_ts, analysis_ts, calendar_limit, weekday_limit):
                result["reasons"].append("quote_stale")
    return _finish(result)


def _point(raw: Any) -> tuple[Any, Any]:
    if isinstance(raw, Mapping):
        return raw.get("ts"), raw.get("value")
    if isinstance(raw, (tuple, list)) and len(raw) >= 2:
        return raw[0], raw[1]
    return getattr(raw, "ts", None), getattr(raw, "value", None)


def assess_history_freshness(
    points: Iterable[Any],
    *,
    analysis_time: Any,
    category: str,
    cadence: str,
    role: str,
) -> dict[str, Any]:
    """Validate raw daily/weekly source observations before they feed the model.

    Points may be objects exposing ``ts``/``value``, mappings, or pairs. Input
    must be chronological with unique timestamps and positive finite numeric
    values. No corrupt observation is dropped to manufacture a passing series.
    Two distinct observations are needed to check the tail cadence; this never
    replaces the downstream requirement for 52 aligned weekly risk returns.
    ``role`` selects stable evidence prefixes: signal, risk, or fx.
    """
    result, analysis_ts = _assessment(analysis_time)
    result.update({
        "role": role,
        "cadence": cadence,
        "observations": 0,
        "distinct_observations": 0,
        "source_first_ts": None,
        "latest_interval_seconds": None,
    })
    if role not in {"signal", "risk", "fx"}:
        result["reasons"].append("history_role_unsupported")
        return _finish(result)
    prefix = f"{role}_history"
    if cadence not in {"daily", "weekly"}:
        result["reasons"].append(f"{prefix}_cadence_unsupported")
        return _finish(result)
    calendar_limit, weekday_limit = _limits(category, cadence)
    result.update({
        "max_calendar_age_seconds": calendar_limit,
        "max_weekday_age_seconds": weekday_limit,
    })
    if not isinstance(points, Iterable) or isinstance(points, (str, bytes, Mapping)):
        result["reasons"].append(f"{prefix}_invalid_container")
        return _finish(result)
    stamps: set[float] = set()
    previous_ts = None
    for raw in points:
        result["observations"] += 1
        raw_ts, raw_value = _point(raw)
        source_ts = _timestamp(raw_ts)
        if source_ts is None:
            result["reasons"].append(
                f"{prefix}_timestamp_missing" if raw_ts is None else f"{prefix}_timestamp_invalid"
            )
        else:
            if source_ts in stamps:
                result["reasons"].append(f"{prefix}_duplicate_timestamp")
            if previous_ts is not None and source_ts < previous_ts:
                result["reasons"].append(f"{prefix}_not_chronological")
            if analysis_ts is not None and source_ts > analysis_ts:
                result["reasons"].append(f"{prefix}_timestamp_future")
            stamps.add(source_ts)
            previous_ts = source_ts
        if _positive_value(raw_value) is None:
            result["reasons"].append(f"{prefix}_value_invalid")
    ordered = sorted(stamps)
    result["distinct_observations"] = len(ordered)
    if not result["observations"]:
        result["reasons"].append(f"{prefix}_empty")
    elif len(ordered) < 2:
        result["reasons"].append(f"{prefix}_insufficient_observations")
    if ordered:
        latest = ordered[-1]
        result["source_first_ts"] = int(ordered[0])
        result["source_as_of_ts"] = int(latest)
        if analysis_ts is not None:
            result["source_age_seconds"] = analysis_ts - latest
            if latest <= analysis_ts and _age_exceeds_policy(
                latest, analysis_ts, calendar_limit, weekday_limit
            ):
                result["reasons"].append(f"{prefix}_stale")
    if len(ordered) >= 2:
        previous, latest = ordered[-2:]
        result["latest_interval_seconds"] = latest - previous
        if _age_exceeds_policy(previous, latest, calendar_limit, weekday_limit):
            result["reasons"].append(f"{prefix}_tail_gap")
    return _finish(result)


def _materialized_points(points: Any) -> list[Any] | None:
    if not isinstance(points, Iterable) or isinstance(points, (str, bytes, Mapping)):
        return None
    return list(points)


def convert_history_with_observed_fx(
    points: Iterable[Any],
    fx_points: Iterable[Any],
    *,
    analysis_time: Any,
    cadence: str = "daily",
    scale: float = 1.0,
) -> tuple[list[tuple[float, float]], dict[str, Any]]:
    """Convert prices using only preceding, sufficiently recent observed FX.

    ``cadence`` describes the FX feed. This strict path accepts ungrouped daily
    FX only: a weekly aggregate labelled with its start is not an observation
    known at that start. The caller must request raw daily FX from the source.
    Currency identity and asset-history freshness are independent caller gates.

    Each raw FX point must pass the FX source-time gate. Each asset point needs
    a valid, positive value and a unique, ascending, non-future timestamp. A rate
    may be carried forward (rate timestamp <= asset timestamp) only within the
    daily exchange/FX policy. Interior or trailing coverage gaps fail
    the entire conversion; we never invent rate 1 or borrow future observations.

    Only asset observations strictly before the first available FX observation
    may be cropped. Their count is explicit; the 52 aligned-return minimum must
    still be applied to the converted series by the caller.
    """
    result, analysis_ts = _assessment(analysis_time)
    result.update({
        "role": "fx_conversion",
        "cadence": cadence,
        "input_observations": 0,
        "converted_observations": 0,
        "dropped_leading_observations": 0,
        "asset_source_as_of_ts": None,
        "max_asof_gap_seconds": None,
        "fx_evidence": None,
    })
    if cadence != "daily":
        result["reasons"].append("fx_history_raw_daily_required")
    scale_value = _positive_value(scale)
    if scale_value is None:
        result["reasons"].append("fx_conversion_scale_invalid")
    raw_assets = _materialized_points(points)
    if raw_assets is None:
        result["reasons"].append("asset_history_invalid_container")
    elif not raw_assets:
        result["reasons"].append("asset_history_empty")
    else:
        result["input_observations"] = len(raw_assets)
    if result["reasons"]:
        return [], _finish(result)

    # Validation precedes cropping: bad leading asset rows cannot disappear as
    # a side effect of missing FX coverage.
    assets: list[tuple[float, float]] = []
    stamps: set[float] = set()
    previous_ts = None
    for raw in raw_assets:
        raw_ts, raw_value = _point(raw)
        source_ts = _timestamp(raw_ts)
        value = _positive_value(raw_value)
        if source_ts is None:
            result["reasons"].append(
                "asset_history_timestamp_missing" if raw_ts is None else "asset_history_timestamp_invalid"
            )
        else:
            if source_ts in stamps:
                result["reasons"].append("asset_history_duplicate_timestamp")
            if previous_ts is not None and source_ts < previous_ts:
                result["reasons"].append("asset_history_not_chronological")
            if source_ts > analysis_ts:
                result["reasons"].append("asset_history_timestamp_future")
            stamps.add(source_ts)
            previous_ts = source_ts
        if value is None:
            result["reasons"].append("asset_history_value_invalid")
        if source_ts is not None and value is not None:
            assets.append((source_ts, value))
    if stamps:
        result["asset_source_as_of_ts"] = int(max(stamps))

    raw_fx = _materialized_points(fx_points)
    fx_evidence = assess_history_freshness(
        raw_fx, analysis_time=analysis_time, category="fx", cadence="daily", role="fx"
    )
    result["fx_evidence"] = fx_evidence
    result["source_as_of_ts"] = fx_evidence["source_as_of_ts"]
    result["source_age_seconds"] = fx_evidence["source_age_seconds"]
    result["reasons"].extend(fx_evidence["reasons"])
    if result["reasons"]:
        return [], _finish(result)

    observed_fx = [(_timestamp(_point(raw)[0]), _positive_value(_point(raw)[1])) for raw in raw_fx]
    converted: list[tuple[float, float]] = []
    calendar_limit, weekday_limit = _limits("fx", "daily")
    result["max_calendar_age_seconds"] = calendar_limit
    result["max_weekday_age_seconds"] = weekday_limit
    index = -1
    max_gap = 0.0
    for stamp, price in assets:
        while index + 1 < len(observed_fx) and observed_fx[index + 1][0] <= stamp:
            index += 1
        if index < 0:
            result["dropped_leading_observations"] += 1
            continue
        rate_ts, rate = observed_fx[index]
        if _age_exceeds_policy(rate_ts, stamp, calendar_limit, weekday_limit):
            result["reasons"].append("fx_history_asof_gap")
            result["uncovered_asset_ts"] = int(stamp)
            result["last_observed_fx_ts"] = int(rate_ts)
            result["uncovered_asof_gap_seconds"] = stamp - rate_ts
            return [], _finish(result)
        value = price * rate * scale_value
        if not math.isfinite(value) or value <= 0:
            result["reasons"].append("fx_converted_value_invalid")
            return [], _finish(result)
        max_gap = max(max_gap, stamp - rate_ts)
        converted.append((stamp, value))
    if not converted:
        result["reasons"].append("fx_history_no_asof_coverage")
    else:
        result["converted_observations"] = len(converted)
        result["max_asof_gap_seconds"] = max_gap
    return (converted if not result["reasons"] else []), _finish(result)
