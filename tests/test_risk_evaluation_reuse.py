"""Dated final-risk arithmetic must stay exact when aligned returns are reused."""
from copy import deepcopy
from datetime import date, timedelta
from fractions import Fraction
import importlib
import json
import math
from pathlib import Path
import sys
from types import ModuleType

import pytest


COMP = Path(__file__).resolve().parents[1] / "custom_components/investment"
PACKAGE = "_risk_reuse_test_package"
package = ModuleType(PACKAGE)
package.__path__ = [str(COMP)]
sys.modules[PACKAGE] = package
plan = importlib.import_module(f"{PACKAGE}.portfolio_plan")
model = importlib.import_module(f"{PACKAGE}.validated_model")


def dated_row(name, values, *, start=date(2023, 1, 2)):
    return {
        "provider": "fixture", "provider_id": name, "economic_sleeve": "broad_equity",
        "risk_weekly_returns": {
            (start + timedelta(weeks=index)).strftime("%G-W%V"): value
            for index, value in enumerate(values)
        },
    }


def exact_tail_loss(values):
    remaining = Fraction(1, 20)
    total = Fraction(0)
    for value in sorted(values):
        mass = min(remaining, Fraction(1, len(values)))
        total += value * mass
        remaining -= mass
        if not remaining:
            break
    return float(max(Fraction(0), -total * 20))


@pytest.mark.parametrize("count", [52, 53, 104, 156])
def test_snapshot_statistics_match_independent_weighted_return_arithmetic(count):
    series = [
        [Fraction((index * multiplier + offset) % 23 - 11, 400) for index in range(count)]
        for multiplier, offset in ((1, 0), (3, 2), (7, 1))
    ]
    weights = [Fraction(1, 10), Fraction(1, 5), Fraction(3, 10)]
    values = [(dated_row(str(index), list(map(float, column))), float(weight) * 1000)
              for index, (column, weight) in enumerate(zip(series, weights, strict=True))]
    snapshot, _, issues = plan._snapshot(values, 400., as_of=date(2026, 10, 6), require_drawdown=True)
    assert issues == []
    returns = [sum(column[index] * weight for column, weight in zip(series, weights, strict=True))
               for index in range(count)]
    mean = sum(returns) / count
    variance = sum((value - mean) ** 2 for value in returns) / count
    assert snapshot["annualized_volatility_3y"] == pytest.approx(math.sqrt(float(variance) * 52), abs=1e-15)
    assert snapshot["expected_shortfall_95_weekly_3y"] == pytest.approx(exact_tail_loss(returns), abs=1e-15)
    wealth = peak = Fraction(1)
    drawdown = Fraction(0)
    for value in returns:
        wealth *= 1 + value
        peak = max(peak, wealth)
        drawdown = max(drawdown, (peak - wealth) / peak)
    assert snapshot["max_drawdown_3y"] == pytest.approx(float(drawdown), abs=1e-15)
    assert snapshot["weekly_observations_3y"] == count


def test_common_calendar_is_intersected_before_return_statistics():
    left = dated_row("A", [.01] * 104)
    right = dated_row("B", [-.02] * 104, start=date(2024, 1, 1))
    snapshot, _, issues = plan._snapshot([(left, 200.), (right, 300.)], 500.,
                                       as_of=date(2026, 10, 6), require_drawdown=True)
    assert not issues
    assert snapshot["weekly_observations_3y"] == 52
    assert snapshot["annualized_volatility_3y"] == 0.
    assert snapshot["expected_shortfall_95_weekly_3y"] == pytest.approx(.004)


@pytest.mark.parametrize("required", [False, True])
def test_calendar_gap_still_hides_path_drawdown_and_blocks_only_an_explicit_path_gate(required):
    row = dated_row("A", [-.001] * 104)
    row["risk_weekly_returns"].pop("2023-W20")
    snapshot, _, issues = plan._snapshot([(row, 200.)], 800.,
                                       as_of=date(2026, 10, 6), require_drawdown=required)
    assert snapshot["weekly_observations_3y"] == 103
    assert snapshot["max_drawdown_3y"] is None
    assert snapshot["expected_shortfall_95_weekly_3y"] == pytest.approx(.0002)
    assert ("portfolio_drawdown_calendar_gaps" in issues) is required


@pytest.mark.parametrize("corruption", ["future", "invalid_week", "nonfinite", "boolean", "too_short"])
def test_reused_statistics_never_certify_invalid_dated_evidence(corruption):
    row = dated_row("A", [.001] * 104)
    if corruption == "future":
        row["risk_weekly_returns"]["2027-W01"] = .1
    elif corruption == "invalid_week":
        row["risk_weekly_returns"]["not-a-week"] = .1
    elif corruption == "too_short":
        row = dated_row("A", [.001] * 51)
    else:
        row["risk_weekly_returns"]["2023-W20"] = math.nan if corruption == "nonfinite" else True
    snapshot, _, issues = plan._snapshot([(row, 200.)], 800.,
                                       as_of=date(2026, 10, 6), require_drawdown=True)
    assert issues
    assert snapshot["annualized_volatility_3y"] is None
    assert snapshot["expected_shortfall_95_weekly_3y"] is None
    json.dumps(snapshot, allow_nan=False)


def test_changed_history_is_not_hidden_by_a_previous_evaluation():
    row = dated_row("A", [.001] * 104)
    before = deepcopy(row)
    initial, _, issues = plan._snapshot([(row, 200.)], 800., as_of=None, require_drawdown=True)
    assert not issues and row == before
    row["risk_weekly_returns"]["2023-W20"] = -.8
    changed, _, issues = plan._snapshot([(row, 200.)], 800., as_of=None, require_drawdown=True)
    assert not issues
    assert changed["expected_shortfall_95_weekly_3y"] > initial["expected_shortfall_95_weekly_3y"]
    assert changed["max_drawdown_3y"] > initial["max_drawdown_3y"]


@pytest.mark.parametrize("corrupt", [None, {}, [True] * 52, [math.inf] * 52, [-1.01] * 52])
def test_standalone_numeric_statistics_fail_closed(corrupt):
    result = model.risk_signature_from_returns(corrupt)
    assert result["annualized_volatility_3y"] is None
    assert result["expected_shortfall_95_weekly_3y"] is None
    assert not model.within_risk_target(result, "very_high")
    json.dumps(result, allow_nan=False)
