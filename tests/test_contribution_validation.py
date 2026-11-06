"""Independent chronology, funding and immutable-fill checks for the replay."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import math

import pytest

from research import contribution_validation as research


def document(*, start="2022-01-01", end="2024-03-15", crypto=True):
    definitions = {
        "CASH": ("Synthetic EUR Overnight Money Market", "cash_like", "etf", 10.0),
        "BOND": ("Synthetic Global Aggregate Bond", "aggregate_bond", "etf", 8.0),
        "EQUITY": ("Synthetic MSCI World Broad Equity", "broad_equity", "etf", 20.0),
    }
    if crypto:
        definitions["BTC"] = ("Bitcoin", "crypto", "crypto", 1000.0)
    assets = {}
    for symbol, (name, sleeve, category, price) in definitions.items():
        bars, day = [], date.fromisoformat(start)
        while day <= date.fromisoformat(end):
            if category == "crypto" or day.weekday() < 5:
                stamp = int(datetime.combine(day, datetime.min.time(), timezone.utc).timestamp()) + 12 * 3600
                bars.append({"date": day.isoformat(), "ts": stamp, "close": price, "adjusted_close": price})
            day += timedelta(days=1)
        assets[symbol] = {"name": name, "sleeve": sleeve, "category": category, "currency": "EUR",
                          "inception": bars[0]["date"], "bars": bars}
    return {"kind": "synthetic_adversarial_scenario", "currency": "EUR", "assets": assets}


def config(**kwargs):
    return research.Experiment(start="2024-01-01", end="2024-02-05", **kwargs)


def replace_price(doc, symbol, day, value):
    for bar in doc["assets"][symbol]["bars"]:
        if bar["date"] == day:
            bar["close"] = bar["adjusted_close"] = value
            return
    raise AssertionError("Fixture date is unavailable")


def manual_order(symbol, sleeve, returns, *, cutoff="2024-01-31", amount=40.0, price=10.0, category="etf"):
    names = {"broad_equity": "Synthetic MSCI World Broad Equity", "government_bond": "Synthetic Government Treasury Bond",
             "cash_like": "Synthetic EUR Overnight Money Market", "crypto": "Bitcoin"}
    last = date.fromisoformat(cutoff)
    last -= timedelta(days=last.weekday())
    weeks = [(last - timedelta(weeks=len(returns) - 1 - index)).isocalendar() for index in range(len(returns))]
    risk_map = {f"{week.year:04d}-W{week.week:02d}": value for week, value in zip(weeks, returns)}
    return {"provider": "synthetic", "provider_id": symbol, "symbol": symbol,
            "name": names[sleeve], "currency": "EUR", "category": category,
            "economic_sleeve": sleeve, "risk_weekly_returns": risk_map,
            "risk_history_weeks": len(risk_map), "risk_history_eligible": True,
            "market_score": 80.0, "score": 80.0, "confidence": 1.0, "activation": 1.0,
            "allocatable_economic_exposure": True, "allocation_eligible": True,
            "portfolio_price": price, "price": price,
            "suggested_amount": amount, "suggested_units": amount / price, "whole_units_only": False}


@pytest.mark.parametrize("changes", [
    {"contribution": True}, {"contribution": 100.001}, {"initial_cash": -1},
    {"min_confidence": float("nan")}, {"max_candidate_fraction": 0},
    {"minimum_cash_reserve_fraction": 1.1}, {"max_drawdown_loss": 0},
    {"fixed_fee": -1}, {"execution_bps": True}, {"whole_units_only": 1},
    {"whole_unit_categories": ["etf", "etf"]}, {"whole_unit_categories": ["invalid"]},
    {"existing_instruments": "unknown"}, {"initial_positions": [("CASH", 1), ("CASH", 2)]},
    {"initial_positions": [("CASH", True)]}, {"strategy": "optimized_after_results"},
])
def test_invalid_experiment_policy_fails_before_replay(changes):
    with pytest.raises((TypeError, ValueError)):
        config(**changes)


def test_nested_experiment_choices_are_immutable():
    cats, positions = ["etf"], [["CASH", 2.0]]
    chosen = config(whole_unit_categories=cats, initial_positions=positions)
    cats.append("crypto")
    positions[0][1] = 99.0
    assert chosen.whole_unit_categories == ("etf",)
    assert chosen.initial_positions == (("CASH", 2.0),)


def test_legacy_holdings_and_cash_change_outcomes_but_not_new_purchase_authority():
    doc = document()
    ordinary = research.replay(doc, config())
    legacy = research.replay(doc, config(initial_cash=10000.0, initial_positions=(("EQUITY", 1000.0),)))
    assert [entry["orders"] for entry in ordinary["trace"]] == [entry["orders"] for entry in legacy["trace"]]
    assert [entry["candidate_history"] for entry in ordinary["trace"]] == [entry["candidate_history"] for entry in legacy["trace"]]
    assert ordinary["trades"] == legacy["trades"]
    assert legacy["metrics"]["ending_nominal_wealth"] - ordinary["metrics"]["ending_nominal_wealth"] == pytest.approx(30000)
    assert all(entry["quote_budget"] == 100 for entry in legacy["trace"])
    for entry in legacy["trace"]:
        for guard in (entry["quote_guard"], entry["fill_guard"]):
            assert guard["portfolio_risk"]["scope"] == "new_contribution_only"
            assert guard["portfolio_risk"]["existing_value"] == 0
            assert guard["portfolio_risk"]["existing_cash"] == 0


def test_unused_cash_is_never_reoffered_to_make_a_later_trade_affordable():
    result = research.replay(document(), research.Experiment(
        strategy="cost_cap_1pct", start="2024-01-01", end="2024-03-15",
        contribution=100.0, initial_cash=1000.0,
    ))
    # Even without spread, a EUR1 fee on each <=EUR45 candidate exceeds1%.
    # More old cash must not quietly change the new contribution's authority.
    assert result["trades"] == []
    assert result["metrics"]["contributions"] == 300
    assert result["metrics"]["ending_cash"] == 1300
    assert [entry["quote_budget"] for entry in result["trace"]] == [100, 100, 100]
    assert [entry["prior_unspent_cash_not_reoffered"] for entry in result["trace"]] == [1000, 1100, 1200]


def test_exact_owned_filter_is_the_only_permitted_use_of_existing_quantities():
    result = research.replay(document(), config(existing_instruments="exclude", initial_positions=(("CASH", 1.0),)))
    purchased = [trade["symbol"] for trade in result["trades"]]
    assert "CASH" not in purchased
    assert len(purchased) == len(set(purchased))
    assert any(row.get("excluded_reason") == "already_owned_exact_source_symbol"
               for entry in result["trace"] for row in entry["candidate_history"])
    assert result["metrics"]["contributions"] == 200


def test_month_end_cutoff_is_independent_of_crypto_weekend_and_filldate():
    panel = research.PricePanel(document())
    schedule = research.monthly_schedule(panel, config())
    assert schedule[0]["decision_session"] == "2023-12-31"  # Sunday
    assert schedule[0]["execution_session"] == "2024-01-01"  # Synthetic exchange calendar
    candidates, exclusions = research.prepared_candidates(panel, "2023-12-31")
    assert not exclusions
    by_symbol = {row["symbol"]: row for row in candidates}
    assert by_symbol["BTC"]["last_observed_session"] == "2023-12-31"
    assert by_symbol["CASH"]["last_observed_session"] == "2023-12-29"
    assert by_symbol["BTC"]["research_signal_first"] == "2022-12-31"
    assert by_symbol["BTC"]["research_signal_observations"] == 366
    # Counters inside the frozen scaffold remain observation based, just as
    # they are in production; the replay does not truncate crypto to253bars.
    assert by_symbol["BTC"]["research_scaffold_result"]["metrics"]["history_observations"] == 366


def test_cached_candidate_preparation_equals_the_uncached_reference():
    panel = research.PricePanel(document())
    cached = research.prepared_candidates(panel, "2023-12-31")
    uncached = research.prepared_candidates(panel, "2023-12-31", use_cache=False)
    assert cached == uncached
    assert research.prepared_candidates(panel, "2024-01-31") == research.prepared_candidates(
        panel, "2024-01-31", use_cache=False,
    )
    assert cached[0][0]["last_observed_session"] <= "2023-12-31"


def test_mutating_returned_candidates_cannot_contaminate_later_cached_runs():
    panel = research.PricePanel(document())
    baseline, exclusions = research.prepared_candidates(panel, "2023-12-31")
    expected = deepcopy((baseline, exclusions))
    baseline[0]["market_score"] = 0
    baseline[0]["risk_weekly_returns"].clear()
    baseline[0]["research_scaffold_result"]["metrics"]["return_126d"] = 1000
    baseline[0]["research_source_quality"]["quote"]["reasons"].append("injected")
    exclusions.append({"symbol": "injected"})
    assert research.prepared_candidates(panel, "2023-12-31") == expected
    assert research.prepared_candidates(panel, "2023-12-31", use_cache=False) == expected


def test_panel_snapshot_does_not_alias_caller_or_returned_history_objects():
    original = document()
    panel = research.PricePanel(original)
    expected = research.prepared_candidates(panel, "2023-12-31")
    original["assets"]["BTC"]["bars"][-1]["close"] = 0
    original["assets"]["CASH"]["name"] = "Changed name"
    history = panel.history("BTC", "2023-12-31")
    history[-1]["close"] = 0
    history[-1]["adjusted_close"] = 0
    assert research.prepared_candidates(panel, "2023-12-31", use_cache=False) == expected


def test_cached_panel_is_shared_safely_across_challenger_and_control_runs():
    panel = research.PricePanel(document())
    chosen = research.Experiment(start="2024-01-01", end="2024-01-03")
    before = research.replay(panel, chosen)
    research.replay(panel, replace(chosen, strategy="no_rsi"))
    research.replay(panel, replace(chosen, strategy="sma10_equal_sleeves"))
    after = research.replay(panel, chosen)
    assert before == after


def test_future_observations_cannot_change_an_earlier_decision_or_fill():
    original = document()
    changed = deepcopy(original)
    for asset in changed["assets"].values():
        for bar in asset["bars"]:
            if bar["date"] >= "2024-01-15":
                bar["close"] *= 10
                bar["adjusted_close"] *= 10
    first = research.replay(original, config())
    second = research.replay(changed, config())
    assert first["trace"][0] == second["trace"][0]


def test_later_crypto_close_cannot_resize_or_cancel_earlier_exchange_fills():
    original = document()
    changed = deepcopy(original)
    replace_price(changed, "BTC", "2024-01-01", 0.01)
    chosen = research.Experiment(start="2024-01-01", end="2024-01-01", execution_bps=25.0)
    first = research.replay(original, chosen)
    second = research.replay(changed, chosen)
    before = [trade for trade in first["trades"] if trade["stage"] == "exchange_close"]
    after = [trade for trade in second["trades"] if trade["stage"] == "exchange_close"]
    assert before
    assert before == after
    assert first["trace"][0]["orders"] == second["trace"][0]["orders"]
    assert first["trace"][0]["fill_guard"]["stages"][0] == second["trace"][0]["fill_guard"]["stages"][0]
    assert first["trace"][0]["fill_guard"]["stages"][0]["execution_timestamp"] == "2024-01-01T16:30:00+00:00"
    assert first["trace"][0]["fill_guard"]["stages"][1]["execution_timestamp"] == "2024-01-01T23:59:59+00:00"


@pytest.mark.parametrize("new_price", [0.123, 5.0, 25.0, 10000.0])
def test_next_price_gap_never_increases_approved_units_or_principal(new_price):
    doc = document(crypto=False)
    panel = research.PricePanel(doc)
    chosen = config(whole_units_only=True, fixed_fee=0.0, execution_bps=0.0)
    orders, _ = research.decide(panel, "2023-12-31", chosen)
    changed = deepcopy(doc)
    replace_price(changed, "CASH", "2024-01-01", new_price)
    balance, trades, _, guard, _ = research.execute_orders(
        orders, research.PricePanel(changed), "2024-01-01", "2023-12-31", chosen,
    )
    approved = {row["symbol"]: row for row in orders}
    for trade in trades:
        assert Decimal(str(trade["units"])) <= Decimal(str(approved[trade["symbol"]]["suggested_units"]))
        exact_principal = Decimal(str(trade["units"])) * Decimal(str(trade["price"]))
        assert exact_principal <= Decimal(str(approved[trade["symbol"]]["suggested_amount"]))
        assert trade["units"] == int(trade["units"])
    assert balance >= 0
    assert guard["portfolio_risk"]["status"] == "within_limits"


def test_whole_fund_policy_leaves_crypto_fractional():
    result = research.replay(document(), config(contribution=1000, whole_unit_categories=("etf",),
                                               fixed_fee=0.0, execution_bps=0.0))
    assert any(trade["symbol"] == "BTC" for trade in result["trades"])
    for trade in result["trades"]:
        if trade["symbol"] == "BTC":
            assert not trade["whole_units_only"]
            assert trade["units"] != int(trade["units"])
        else:
            assert trade["whole_units_only"]
            assert trade["units"] == int(trade["units"])


def test_missing_fill_omits_order_and_rechecks_risk_after_removing_hedge():
    doc = document(end="2024-02-02", crypto=False)
    doc["assets"]["BOND"]["bars"] = [bar for bar in doc["assets"]["BOND"]["bars"] if bar["date"] != "2024-02-01"]
    a = [-.1, .1] * 26
    orders = [manual_order("EQUITY", "broad_equity", a),
              manual_order("BOND", "government_bond", [-value for value in a])]
    balance, trades, rejected, guard, _ = research.execute_orders(
        orders, research.PricePanel(doc), "2024-02-01", "2024-01-31",
        config(fixed_fee=0, execution_bps=0),
    )
    assert any(row["symbol"] == "BOND" and row["reason"] == "no_execution_observation" for row in rejected)
    assert [trade["symbol"] for trade in trades] == ["EQUITY"]
    assert trades[0]["principal"] < 30  # Ungated40% would exceed16% annual volatility.
    assert guard["portfolio_risk"]["post"]["annualized_volatility_3y"] <= .16 * 1.01 + 1e-12
    assert balance > 70


def test_cost_removal_and_risk_reduction_repeat_until_cost_policy_holds():
    doc = document(end="2024-02-02", crypto=False)
    a = [-.5, .5] * 26
    orders = [manual_order("EQUITY", "broad_equity", a, amount=150, price=20),
              manual_order("BOND", "government_bond", [-value * 150 / 105 for value in a], amount=105, price=8)]
    balance, trades, _, guard, filters = research.execute_orders(
        orders, research.PricePanel(doc), "2024-02-01", "2024-01-31",
        config(strategy="cost_cap_1pct", contribution=1000),
    )
    # The low-notional hedge fails the fee cap. Its removal shrinks the other
    # order enough that it also becomes uneconomic. Neither can be resurrected.
    removed = [row["symbol"] for detail in filters for row in detail.get("removed", [])]
    assert "BOND" in removed
    assert "EQUITY" in removed
    assert not trades
    assert balance == 1000
    assert guard["portfolio_risk"]["post"]["cash_weight"] == 1.0


def test_flow_unit_issue_uses_previous_day_marks_before_execution_day_gap():
    doc = document()
    replace_price(doc, "BTC", "2024-01-01", 2000)
    result = research.replay(doc, research.Experiment(
        strategy="cash_only", start="2024-01-01", end="2024-01-01", initial_positions=(("BTC", 1.0),),
    ))
    point = result["daily_accounting"][0]
    entry = result["trace"][0]
    assert result["metrics"]["initial_capital"] == 1000
    assert entry["pre_flow_nav"] == 1
    assert entry["pre_flow_wealth"] == 1000
    assert point["issued_units"] == 1100
    assert point["wealth"] == 2100
    assert point["nav"] == pytest.approx(2100 / 1100)


def test_exact_flat_price_accounting_and_cash_flow_unitization(monkeypatch):
    chosen = config(fixed_fee=1.0, execution_bps=0.0)

    def fixed_one_unit(panel, cutoff, config, *, owned_symbols=()):
        rows = [manual_order("CASH", "cash_like", [0.0] * 52, cutoff=cutoff, amount=10, price=10)]
        rows, guard = research._finalize(rows, config, cutoff)
        return rows, {"candidate_history": [], "quote_guard": guard, "policy": {}}

    monkeypatch.setattr(research, "decide", fixed_one_unit)
    result = research.replay(document(crypto=False), chosen)
    assert result["metrics"]["contributions"] == 200
    assert result["metrics"]["total_execution_costs"] == 2
    assert result["metrics"]["ending_nominal_wealth"] == 198
    assert result["metrics"]["ending_cash"] == 178
    assert result["ending_positions"] == {"CASH": 2}
    assert result["daily_accounting"][-1]["issued_units"] == pytest.approx(100 + 100 / .99)
    assert result["daily_accounting"][-1]["nav"] == pytest.approx(198 / (100 + 100 / .99))


def test_subcent_price_keeps_marked_principal_separate_from_cash_rounding():
    fee, rounding, debit = research.Costs(fixed_fee=.01, execution_bps=0).exact_charge(Decimal("4.914"))
    assert fee == Decimal(".01")
    assert rounding == Decimal(".006")
    assert debit == Decimal("4.93")


def test_performance_uses_full_calendar_weeks_and_cash_flow_adjusted_nav():
    points = []
    first = date(2024, 1, 1)
    for index in range(22):
        nav = 1 + .01 * index
        points.append({"date": (first + timedelta(days=index)).isoformat(),
                       "nav": nav, "wealth": nav * 100, "cash": 0})
    stats = research.unitized_metrics(points)
    # Complete Sunday values:1.06,1.13,1.20. Jan22 is a partial fourthweek.
    weekly = [1.13 / 1.06 - 1, 1.20 / 1.13 - 1]
    expected = abs(weekly[0] - weekly[1]) / 2 * math.sqrt(52)
    assert stats["weekly_observations"] == 2
    assert stats["annualized_weekly_volatility"] == pytest.approx(expected)
    assert stats["time_weighted_total_return_after_costs"] == pytest.approx(.21)


@pytest.mark.parametrize("strategy", research.STRATEGIES)
def test_late_inception_stays_unavailable_until_shared_risk_history_exists(strategy):
    doc = document()
    doc["assets"]["EQUITY"]["inception"] = "2023-12-20"
    result = research.replay(doc, research.Experiment(strategy=strategy, start="2024-01-01", end="2024-01-02"))
    assert all(trade["symbol"] != "EQUITY" for trade in result["trades"])
    row = next(row for row in result["trace"][0]["candidate_history"] if row["symbol"] == "EQUITY")
    assert row["weekly_count"] < 52
    assert row["allocation_weight_after_constraints"] == 0


def test_unavailable_common_session_retains_monthly_cash_flow():
    doc = document(crypto=False)
    for asset in doc["assets"].values():
        asset["bars"] = [bar for bar in asset["bars"] if not bar["date"].startswith("2024-01")]
    chosen = research.Experiment(strategy="cash_only", start="2024-01-01", end="2024-01-03")
    result = research.replay(doc, chosen)
    assert result["metrics"]["contributions"] == 100
    assert result["metrics"]["ending_cash"] == 100
    assert result["trace"][0]["execution_rejections"] == [{"symbol": None, "reason": "no_common_observed_execution_session"}]


def test_source_ending_before_requested_replay_is_rejected():
    with pytest.raises(ValueError, match="final observation"):
        research.replay(document(end="2024-01-03"), config())


def test_different_quote_currency_is_not_silently_converted_at_one():
    doc = document()
    doc["assets"]["CASH"]["currency"] = "USD"
    with pytest.raises(ValueError, match="historical FX"):
        research.PricePanel(doc)
