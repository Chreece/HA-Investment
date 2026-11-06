"""Independent accounting and provenance audit of contribution-only studies.

The oracle reconstructs balances, quantities, cash flows, NAV, source-calendar
availability and historical plan risk directly from prices and recorded orders.
It does not import the replay's decision, execution or accounting functions.
Synthetic fixture generation is used only to verify a frozen dataset's origin.
An audit pass establishes these implementation invariants, not future returns.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from calendar import monthrange
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, localcontext
import gzip
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any
from zoneinfo import ZoneInfo


SCHEMA = "ha-investment-contribution-independent-audit-v1"
REPO = Path(__file__).resolve().parents[1]
CENT = Decimal("0.01")
ZERO = Decimal(0)
VOL_TARGETS = {"very_low": .05, "low": .10, "medium": .16, "high": .25, "very_high": .40}
SLEEVE_CAPS = {
    "very_low": dict(cash_like=.55, government_bond=.35, aggregate_bond=.30,
                     broad_equity=.20, sector_equity=.05, single_equity=.05, commodity=.05, crypto=0.0),
    "low": dict(cash_like=.50, government_bond=.45, aggregate_bond=.40,
                broad_equity=.35, sector_equity=.10, single_equity=.15, commodity=.10, crypto=.03),
    "medium": dict(cash_like=.45, government_bond=.45, aggregate_bond=.45,
                   broad_equity=.55, sector_equity=.25, single_equity=.30, commodity=.15, crypto=.10),
    "high": dict(cash_like=.40, government_bond=.40, aggregate_bond=.45,
                 broad_equity=.70, sector_equity=.45, single_equity=.50, commodity=.25, crypto=.25),
    "very_high": dict(cash_like=.35, government_bond=.35, aggregate_bond=.40,
                      broad_equity=.85, sector_equity=.70, single_equity=.75, commodity=.40, crypto=.40),
}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("Expected an actual numeric ledger value")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Nonfinite ledger value")
    return number


def _date(value: Any) -> date:
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError("Noncanonical observation date")
    return result


def _close(left: Any, right: Any, *, atol=1e-8, rtol=1e-10) -> bool:
    a, b = _decimal(left), _decimal(right)
    return abs(a - b) <= Decimal(str(atol)) + Decimal(str(rtol)) * max(abs(a), abs(b))


class _Source:
    """The auditor's own source index; it has no strategy-dependent behavior."""

    def __init__(self, document):
        self.assets = document["assets"]
        if not self.assets:
            raise ValueError("Empty audit source")
        self.rows, self.days = {}, {}
        self.weekly_cache = {}
        for symbol, asset in self.assets.items():
            if asset.get("currency") != document.get("currency", "EUR"):
                raise ValueError("Source requires an undeclared currency conversion")
            inception = _date(asset["inception"])
            rows, previous, previous_stamp = [], None, None
            for row in asset["bars"]:
                day, stamp = _date(row["date"]), _decimal(row["ts"])
                if previous is not None and (day <= previous or stamp <= previous_stamp):
                    raise ValueError("Source dates/timestamps are not strictly increasing")
                previous, previous_stamp = day, stamp
                if (_decimal(row["close"]) <= 0 or _decimal(row["adjusted_close"]) <= 0
                        or stamp <= 0):
                    raise ValueError("Nonpositive source price or timestamp")
                if abs((datetime.fromtimestamp(float(stamp), timezone.utc).date() - day).days) > 1:
                    raise ValueError("Source session date contradicts its timestamp")
                if day >= inception:
                    rows.append(row)
            self.rows[symbol] = rows
            self.days[symbol] = [row["date"] for row in rows]

    def history(self, symbol, cutoff):
        return self.rows[symbol][:bisect_right(self.days[symbol], cutoff)]

    def latest(self, symbol, day):
        index = bisect_right(self.days[symbol], day) - 1
        return self.rows[symbol][index] if index >= 0 else None

    def mark(self, symbol, day):
        row = self.latest(symbol, day)
        if row is None:
            raise ValueError(f"Cannot value {symbol} before its first post-inception observation")
        age = (_date(day) - _date(row["date"])).days
        if age > (2 if self.assets[symbol]["category"] == "crypto" else 7):
            raise ValueError(f"Stale valuation of {symbol} on {day}")
        return _decimal(row["close"])

    def weekly_returns(self, symbol, cutoff):
        key = symbol, cutoff
        if key in self.weekly_cache:
            return self.weekly_cache[key]
        rows = self.history(symbol, cutoff)
        if not rows:
            return {}
        earliest = float(rows[-1]["ts"]) - 3 * 370 * 86400
        closes = {}
        for row in rows:
            if float(row["ts"]) >= earliest:
                iso = _date(row["date"]).isocalendar()
                closes[date.fromisocalendar(iso.year, iso.week, 1)] = float(row["adjusted_close"])
        weeks = sorted(closes)
        result = {
            later: closes[later] / closes[earlier] - 1.0
            for earlier, later in zip(weeks, weeks[1:]) if (later - earlier).days == 7
        }
        self.weekly_cache[key] = result
        return result

    def schedule(self, parameters):
        start, end = _date(parameters["start"]), _date(parameters["end"])
        first = start.replace(day=1)
        result = {}
        while first <= end:
            cutoff = first - timedelta(days=1)
            following = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
            active = sorted(symbol for symbol, asset in self.assets.items()
                            if _date(asset["inception"]) <= cutoff
                            and self.latest(symbol, cutoff.isoformat()) is not None)
            possible = None
            for symbol in active:
                observed = {day for day in self.days[symbol]
                            if first.isoformat() <= day < following.isoformat()}
                possible = observed if possible is None else possible.intersection(observed)
            if possible:
                session, unavailable = min(possible), None
            else:
                weekday = first
                while weekday.weekday() >= 5:
                    weekday += timedelta(days=1)
                session = weekday.isoformat()
                unavailable = ("no_common_observed_execution_session" if active
                               else "no_incepted_observed_calendar_assets")
            if start.isoformat() <= session <= end.isoformat():
                result[session] = dict(cutoff=cutoff.isoformat(), assets=active, unavailable=unavailable)
            first = following
        return result


def _charges(principal, parameters):
    rate = sum((_decimal(parameters.get(key, 0.0))
                for key in ("commission_bps", "execution_bps", "fx_bps")), ZERO) / 10000
    fee = _decimal(parameters["fixed_fee"]) + principal * rate
    debit = (principal + fee).quantize(CENT, rounding=ROUND_CEILING)
    return fee, debit - principal - fee, debit


def _tail_loss(returns):
    """Integrate the loss distribution directly, including fractional tail mass."""
    if not returns:
        return None
    mass = len(returns) / 20.0
    remaining, total = mass, 0.0
    for loss in sorted((-value for value in returns), reverse=True):
        take = min(1.0, remaining)
        total += loss * take
        remaining -= take
        if remaining <= 1e-12:
            break
    return max(0.0, total / mass)


def _plan_risk(source, purchases, cutoff, parameters, check, checked, label):
    """Numerical oracle for the frozen historical contribution envelope."""
    if not purchases:
        return
    marked = [(row["symbol"], _decimal(row["units"]) * _decimal(row["price"])) for row in purchases]
    spent = sum((_charges(principal, parameters)[2] for _, principal in marked), ZERO)
    budget = _decimal(parameters["contribution"])
    cash = budget - spent
    value = cash + sum((principal for _, principal in marked), ZERO)
    check(value > 0 and cash >= 0, label + ": invalid post-fee contribution value")
    if value <= 0:
        return
    maps = [source.weekly_returns(symbol, cutoff) for symbol, _ in marked]
    weeks = sorted(set.intersection(*(set(values) for values in maps)))
    check(len(weeks) >= 52, label + ": fewer than 52 aligned weekly returns")
    if len(weeks) < 52:
        return
    weights = [float(principal / value) for _, principal in marked]
    returns = [math.fsum(weight * values[week] for weight, values in zip(weights, maps)) for week in weeks]
    target = VOL_TARGETS[parameters["risk"]]
    volatility = statistics.pstdev(returns) * math.sqrt(52.0)
    shortfall = _tail_loss(returns)
    check(volatility <= target * 1.01 + 1e-9, label + ": historical volatility limit exceeded")
    check(shortfall <= 2.0627128075074257 * target / math.sqrt(52.0) * 1.01 + 1e-9,
          label + ": empirical expected-shortfall limit exceeded")
    path, peak, drawdown = 1.0, 1.0, 0.0
    for observation in returns:
        path *= 1.0 + observation
        peak = max(peak, path)
        drawdown = max(drawdown, 1.0 - path / peak)
    if parameters.get("max_drawdown_loss") is not None:
        check(all((later - earlier).days == 7 for earlier, later in zip(weeks, weeks[1:])),
              label + ": disconnected weeks cannot establish historical drawdown")
        check(drawdown <= float(parameters["max_drawdown_loss"]) + 1e-9,
              label + ": explicit historical drawdown limit exceeded")
    sleeves = defaultdict(Decimal)
    for symbol, principal in marked:
        sleeve = source.assets[symbol].get("sleeve")
        check(sleeve in SLEEVE_CAPS[parameters["risk"]], label + ": unknown economic sleeve")
        sleeves[sleeve] += principal
        cap = parameters.get("max_candidate_fraction")
        if cap is not None:
            check(float(principal / value) <= float(cap) + 1e-9, label + ": instrument cap exceeded")
    for sleeve, principal in sleeves.items():
        check(float(principal / value) <= SLEEVE_CAPS[parameters["risk"]].get(sleeve, 0.0) + 1e-9,
              label + ": economic sleeve cap exceeded")
    checked["independent_historical_plan_risk_checks"] += 1


def _audit_trend(source, entry, check, checked):
    """Rebuild the ten completed month ends without importing the trend rule."""
    cutoff = _date(entry["decision_session"])
    months, cursor = [], cutoff
    for _ in range(10):
        months.append(cursor.strftime("%Y-%m"))
        cursor = cursor.replace(day=1) - timedelta(days=1)
    months.reverse()
    eligible = {}
    diagnostics = entry["policy"]["transformations"]["candidate_diagnostics"]
    for detail in diagnostics:
        symbol, signal = detail["symbol"], detail["trend"]
        month_closes = {}
        for row in source.history(symbol, cutoff.isoformat()):
            if row["date"][:7] in months:
                month_closes[row["date"][:7]] = row
        missing = [month for month in months if month not in month_closes]
        stale = []
        for month, row in month_closes.items():
            observed = _date(row["date"])
            if monthrange(observed.year, observed.month)[1] - observed.day > 7:
                stale.append(month)
        observations = [dict(date=month_closes[month]["date"], adjusted_close=month_closes[month]["adjusted_close"])
                        for month in months if month in month_closes]
        check(signal["cutoff"] == cutoff.isoformat() and signal["latest_completed_month"] == months[-1]
              and signal["months_required"] == 10, symbol + ": trend uses an incomplete or wrong month window")
        check(_canonical(signal["observations"]) == _canonical(observations)
              and signal["missing_months"] == missing and signal["stale_months"] == sorted(stale),
              symbol + ": trend observations differ from completed source months")
        selected = False
        if not missing and not stale:
            prices = [float(row["adjusted_close"]) for row in observations]
            average = math.fsum(prices) / 10
            selected = prices[-1] > average
            check(_close(signal["close"], prices[-1]) and _close(signal["sma"], average),
                  symbol + ": trend arithmetic differs")
        else:
            check(signal["close"] is None and signal["sma"] is None,
                  symbol + ": missing months were replaced or imputed")
        check(signal["eligible"] is selected, symbol + ": strict completed-month trend gate differs")
        eligible[symbol] = selected
        checked["completed_month_trend_checks"] += 1
    for order in entry["orders"]:
        if _decimal(order["suggested_units"]) > 0:
            check(eligible.get(order["symbol"]) is True, order["symbol"] + ": approved despite failed completed-month trend")


def _audit_stages(source, entry, parameters, check, checked):
    """Reconcile immutable event prefixes using only each stage's source prices."""
    day, cutoff = entry["execution_session"], entry["decision_session"]
    approved = {row["symbol"]: row for row in entry["orders"]}
    group = lambda symbol: "crypto_close" if source.assets[symbol]["category"] == "crypto" else "exchange_close"
    expected_stages = [name for name in ("exchange_close", "crypto_close")
                       if any(group(symbol) == name for symbol in approved)]
    stages = entry["fill_guard"]["stages"]
    check([stage["stage"] for stage in stages] == expected_stages, day + ": execution stage order differs")
    balance = _decimal(parameters["contribution"])
    reserve = (balance * _decimal(parameters["minimum_cash_reserve_fraction"])).quantize(CENT, rounding=ROUND_CEILING)
    committed, cumulative, flattened = {}, [], []
    for stage in stages:
        name = stage["stage"]
        observed_day = _date(day)
        if name == "exchange_close":
            instant = datetime(observed_day.year, observed_day.month, observed_day.day, 17, 30,
                               tzinfo=ZoneInfo("Europe/Berlin")).astimezone(timezone.utc).isoformat()
        else:
            check(name == "crypto_close", day + ": undeclared execution stage")
            instant = day + "T23:59:59+00:00"
        check(stage["execution_timestamp"] == instant, day + ": execution event timestamp differs")
        check(_close(stage["cash_before"], balance), day + ": stage reused spent or old-account cash")
        check(set(stage["committed_units_before"]) == set(committed)
              and all(_close(stage["committed_units_before"][symbol], units) for symbol, units in committed.items()),
              day + ": earlier fills changed before a later stage")
        stage_orders = {row["symbol"]: row for row in stage["orders"]}
        expected_symbols = {symbol for symbol in approved if group(symbol) == name}
        check(set(stage_orders) == expected_symbols and len(stage_orders) == len(stage["orders"]),
              day + ": stage contains a future or wrong-category order")
        for symbol, order in stage_orders.items():
            original = approved[symbol]
            check(_decimal(order["suggested_units"]) <= _decimal(original["suggested_units"])
                  and _decimal(order["suggested_amount"]) <= _decimal(original["suggested_amount"]),
                  day + ": stage increased original quote authority")
            if _decimal(order["suggested_units"]) > 0:
                row = source.latest(symbol, day)
                check(row is not None and row["date"] == day and _close(order["price"], row["close"]),
                      day + ": stage price is not its own observed close")
        guard = stage["guard"]["portfolio_risk"]
        prefix_value = sum((_decimal(row["units"]) * _decimal(row["price"]) for row in cumulative), ZERO)
        check(guard["scope"] == ("complete_portfolio" if committed else "new_contribution_only")
              and _close(guard["existing_value"], prefix_value) and guard["existing_cash"] == 0
              and _close(guard["purchase_budget"], balance), day + ": stage guard includes old-account state")
        if committed:
            check(stage["guard"].get("research_scope") == "already_filled_positions_within_this_new_contribution_only",
                  day + ": later-stage guard does not identify its temporary contribution bucket")
        for trade in stage["trades"]:
            symbol = trade["symbol"]
            check(symbol in stage_orders and group(symbol) == name and symbol not in committed,
                  day + ": later stage altered or duplicated an earlier fill")
            check(trade["stage"] == name and trade["execution_timestamp"] == instant,
                  day + ": fill timestamp does not match its causal stage")
            units, price = _decimal(trade["units"]), _decimal(trade["price"])
            balance -= _charges(units * price, parameters)[2]
            committed[symbol] = units
            cumulative.append(dict(symbol=symbol, units=units, price=price))
            flattened.append(trade)
        check(balance >= reserve and balance >= 0 and _close(stage["cash_after"], balance),
              day + ": stage cash/reserve does not reconcile")
        check(_close(stage["guard"]["execution_costs"]["cash_remaining"], balance),
              day + ": stage guard cash differs from exact costs")
        _plan_risk(source, cumulative, cutoff, parameters, check, checked, day + ": " + name + " prefix")
        checked["causal_execution_stages"] += 1
    check(_canonical(flattened) == _canonical(entry["trades"]), day + ": causal stage fills differ from decision ledger")
    final_risk = entry["fill_guard"]["portfolio_risk"]
    check(final_risk["read_only_after_execution"] is True,
          day + ": final aggregate guard could retrospectively change earlier fills")
    check(set(final_risk["already_filled_in_this_contribution"]) == set(committed)
          and all(_close(final_risk["already_filled_in_this_contribution"][symbol], units) for symbol, units in committed.items()),
          day + ": final bucket changed committed units")
    check(_close(entry["fill_guard"]["execution_costs"]["cash_remaining"], balance),
          day + ": final guard cash differs from causal stage ledger")


def audit_trial(document, trace):
    """Verify a complete retained trial without trusting its accounting outputs."""
    violations, checked = [], defaultdict(int)

    def check(condition, message):
        if not condition:
            violations.append(message)

    try:
        with localcontext() as context:
            context.prec = 60
            _audit_trial(document, trace, check, checked)
    except (ArithmeticError, AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
        violations.append(f"Malformed source or retained ledger: {type(error).__name__}: {error}")
    return {"schema": SCHEMA, "checked": dict(checked), "violations": violations, "passed": not violations}


def _audit_trial(document, trace, check, checked):
    parameters, metrics = trace["parameters"], trace["metrics"]
    source = _Source(document)
    schedule = source.schedule(parameters)
    start, end = _date(parameters["start"]), _date(parameters["end"])
    before_start = (start - timedelta(days=1)).isoformat()
    check(parameters["end"] <= max(days[-1] for days in source.days.values() if days),
          "trial continues beyond the final source observation")
    expected_dates = [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]
    points = trace["daily_accounting"]
    check([point["date"] for point in points] == expected_dates, "daily accounting has missing, duplicate or extra dates")
    decisions = trace["trace"]
    check([entry["execution_session"] for entry in decisions] == sorted(schedule),
          "monthly execution/flow schedule differs from frozen source-calendar policy")
    by_day = {entry["execution_session"]: entry for entry in decisions}
    check(len(by_day) == len(decisions), "duplicate monthly decision")
    quantities = {symbol: _decimal(quantity) for symbol, quantity in parameters["initial_positions"]}
    check(len(quantities) == len(parameters["initial_positions"]), "duplicate initial holding")
    check(all(quantity >= 0 for quantity in quantities.values()), "negative initial holding")
    cash = _decimal(parameters["initial_cash"])
    initial = cash + sum((quantity * source.mark(symbol, before_start)
                          for symbol, quantity in quantities.items() if quantity > 0), ZERO)
    check(_close(metrics["initial_capital"], initial), "initial capital differs from independently marked holdings/cash")
    issued, last_nav = initial, Decimal(1)
    total_deposits, total_costs, total_principal = ZERO, ZERO, ZERO
    fills, abstentions, rejections = [], 0, 0
    reconstructed = []
    for point in points:
        day = point["date"]
        checked["daily_accounting_points"] += 1
        expected_flow = _decimal(parameters["contribution"]) if day in schedule else ZERO
        check(_decimal(point["contribution"]) == expected_flow, day + ": external contribution differs")
        if expected_flow:
            previous_day = (_date(day) - timedelta(days=1)).isoformat()
            before_wealth = cash + sum((quantity * source.mark(symbol, previous_day)
                                       for symbol, quantity in quantities.items() if quantity > 0), ZERO)
            before_nav = before_wealth / issued if issued > 0 else last_nav
            if before_nav <= 0:
                raise ValueError("A positive flow cannot be unitized at nonpositive NAV")
            entry, scheduled = by_day[day], schedule[day]
            check(entry["contribution_timestamp"] == day + "T00:00:00+00:00",
                  day + ": external contribution did not precede the trading day")
            check(_close(entry["pre_flow_nav"], before_nav, atol=1e-10)
                  and _close(entry["pre_flow_wealth"], before_wealth)
                  and _close(entry["issued_units_before_flow"], issued),
                  day + ": midnight flow used a future mark or wrong prior NAV")
            issued += expected_flow / before_nav
            total_deposits += expected_flow
            cutoff = scheduled["cutoff"]
            checked["decisions"] += 1
            check(entry["decision_session"] == cutoff and cutoff < day, day + ": decision is not completed previous month-end")
            check(entry["calendar_assets"] == scheduled["assets"], day + ": execution universe depends on strategy/holdings")
            check(_decimal(entry["contribution"]) == expected_flow and _decimal(entry["quote_budget"]) == expected_flow,
                  day + ": decision budget exceeds new external contribution")
            check(_close(entry["prior_unspent_cash_not_reoffered"], cash), day + ": old cash does not reconcile")
            history = {row["symbol"]: row for row in entry["candidate_history"]}
            check(len(history) == len(entry["candidate_history"]), day + ": duplicate candidate history")
            year_back = date(_date(cutoff).year - 1, _date(cutoff).month,
                             min(_date(cutoff).day, monthrange(_date(cutoff).year - 1, _date(cutoff).month)[1])).isoformat()
            for symbol, item in history.items():
                check(symbol in source.assets, day + ": candidate absent from frozen universe")
                rows = source.history(symbol, cutoff)
                if "first" in item:
                    check(bool(rows) and item["first"] == rows[0]["date"] and item["last"] == rows[-1]["date"],
                          day + ": candidate history includes future/pre-inception or omitted prices")
                if "signal_first" in item:
                    signal = [row for row in rows if row["date"] >= year_back]
                    check(bool(signal) and item["signal_first"] == signal[0]["date"]
                          and item["signal_last"] == signal[-1]["date"]
                          and item["signal_observations"] == len(signal),
                          day + ": signal history differs from the one-calendar-year contract")
                if "weekly_count" in item:
                    check(item["weekly_count"] == len(source.weekly_returns(symbol, cutoff)),
                          day + ": recorded weekly history count differs")
                if item.get("economic_sleeve") is not None:
                    check(item["economic_sleeve"] == source.assets[symbol].get("sleeve"),
                          day + ": candidate economic sleeve differs from frozen catalog")
            orders = {row["symbol"]: row for row in entry["orders"]}
            check(len(orders) == len(entry["orders"]), day + ": duplicate approved order")
            if parameters["strategy"] == "sma10_equal_sleeves":
                _audit_trend(source, entry, check, checked)
            _audit_stages(source, entry, parameters, check, checked)
            quoted_purchases = []
            for symbol, order in orders.items():
                units, amount = _decimal(order["suggested_units"]), _decimal(order["suggested_amount"])
                check(units >= 0 and amount >= 0 and bool(units) == bool(amount), day + ": inconsistent/negative quote authority")
                row = source.latest(symbol, cutoff)
                check(row is not None and _close(order["price"], row["close"]), day + ": quote price differs from available source")
                whole = parameters["whole_units_only"] or source.assets[symbol]["category"] in parameters["whole_unit_categories"]
                check(order["whole_units_only"] is bool(whole), day + ": wrong quote lot policy")
                if units <= 0:
                    continue
                check(symbol in history and not history[symbol].get("excluded_reason"), day + ": approved candidate was excluded/missing")
                check(history.get(symbol, {}).get("weekly_count", 0) >= 52, day + ": approved candidate has insufficient history")
                check(float(history.get(symbol, {}).get("confidence", -1)) + 1e-15 >= float(parameters["min_confidence"]),
                      day + ": approved candidate fails reliability floor")
                check(units * _decimal(order["price"]) <= amount, day + ": quoted units exceed quoted principal")
                principal = units * _decimal(order["price"])
                if parameters["strategy"] == "cost_cap_1pct":
                    check(_charges(principal, parameters)[2] - principal <= principal / 100,
                          day + ": approved quote exceeds 1 percent cost cap")
                if whole:
                    check(units == units.to_integral_value(), day + ": fractional quote for whole-unit instrument")
                if parameters["existing_instruments"] == "exclude":
                    check(quantities.get(symbol, ZERO) == 0, day + ": already-owned instrument was approved")
                quoted_purchases.append(dict(symbol=symbol, units=units, price=order["price"]))
            quote_spent = sum((_charges(row["units"] * _decimal(row["price"]), parameters)[2]
                               for row in quoted_purchases), ZERO)
            reserve = (expected_flow * _decimal(parameters["minimum_cash_reserve_fraction"])).quantize(CENT, rounding=ROUND_CEILING)
            check(expected_flow - quote_spent >= reserve,
                  day + ": approved quote spent old cash or required reserve")
            check(_close(entry["quote_guard"]["execution_costs"]["cash_remaining"], expected_flow - quote_spent),
                  day + ": quote guard cash differs from exact costs")
            spent, new_fills = ZERO, []
            seen_fills = set()
            if scheduled["unavailable"]:
                check(not quoted_purchases and not entry["trades"], day + ": filled despite unavailable execution calendar")
                check(any(row.get("reason") == scheduled["unavailable"] for row in entry["execution_rejections"]),
                      day + ": missing retained no-common-session explanation")
            for trade in entry["trades"]:
                symbol = trade["symbol"]
                checked["fills"] += 1
                check(symbol not in seen_fills, day + ": duplicate fill for one order")
                seen_fills.add(symbol)
                check(trade["session"] == day and symbol in orders, day + ": fill date/authority missing")
                order = orders[symbol]
                row = source.latest(symbol, day)
                check(row is not None and row["date"] == day, day + ": no observed execution close")
                price, units = _decimal(trade["price"]), _decimal(trade["units"])
                check(row is not None and price == _decimal(row["close"]), day + ": fill uses a fabricated execution price")
                check(units > 0 and price > 0, day + ": nonpositive fill")
                check(units <= _decimal(order["suggested_units"]), day + ": fill increased quote-approved units")
                principal = price * units
                check(principal <= _decimal(order["suggested_amount"]), day + ": fill increased quote-approved principal")
                check(_decimal(trade["approved_units"]) == _decimal(order["suggested_units"])
                      and _decimal(trade["approved_amount"]) == _decimal(order["suggested_amount"]),
                      day + ": fill changed its recorded approval limits")
                whole = parameters["whole_units_only"] or source.assets[symbol]["category"] in parameters["whole_unit_categories"]
                check(trade["whole_units_only"] is bool(whole), day + ": wrong fill lot policy")
                if whole:
                    check(units == units.to_integral_value(), day + ": fractional fill for whole-unit instrument")
                fee, rounding, debit = _charges(principal, parameters)
                check(_close(trade["principal"], principal), day + ": marked principal differs")
                check(_close(trade["fees"], fee), day + ": configured fees differ")
                check(_close(trade["rounding_cost"], rounding, atol=1e-12), day + ": cash-rounding cost differs")
                check(_decimal(trade["cash_debit"]) == debit, day + ": exact cent cash debit differs")
                if parameters["strategy"] == "cost_cap_1pct":
                    check(debit - principal <= principal / 100, day + ": executed purchase exceeds 1 percent cost cap")
                spent += debit
                quantities[symbol] = quantities.get(symbol, ZERO) + units
                total_costs += fee + rounding
                total_principal += principal
                new_fills.append(dict(symbol=symbol, units=units, price=price))
            unused = expected_flow - spent
            check(unused >= reserve and unused >= 0, day + ": purchases spent old cash or required reserve")
            cash += unused
            check(_close(entry["new_contribution_cash_remaining"], unused), day + ": new-contribution cash differs")
            check(_close(entry["cash_after_execution"], cash), day + ": post-trade total cash differs")
            for name, purchases in (("quote_guard", quoted_purchases), ("fill_guard", new_fills)):
                guard = entry[name]["portfolio_risk"]
                check(guard["scope"] == "new_contribution_only" and guard["existing_cash"] == 0
                      and guard["existing_value"] == 0 and _decimal(guard["purchase_budget"]) == expected_flow,
                      day + ": old account state entered " + name)
                if purchases:
                    check(guard["status"] == "within_limits" and not guard["post_breaches"],
                          day + ": positive purchase failed " + name)
                _plan_risk(source, purchases, cutoff, parameters, check, checked, day + ": " + name)
            fills.extend(entry["trades"])
            abstentions += not bool(entry["trades"])
            rejections += len(entry["execution_rejections"])
        positions_value = sum((quantity * source.mark(symbol, day)
                               for symbol, quantity in quantities.items() if quantity > 0), ZERO)
        wealth = cash + positions_value
        nav = wealth / issued if issued > 0 else Decimal(1)
        check(_close(point["cash"], cash), day + ": cash ledger does not reconcile")
        check(_close(point["positions_value"], positions_value), day + ": held-quantity valuation differs")
        check(_close(point["wealth"], wealth), day + ": wealth does not reconcile")
        check(_close(point["issued_units"], issued), day + ": externally funded NAV-share issuance differs")
        check(_close(point["nav"], nav, atol=1e-10), day + ": deposit-neutral NAV differs")
        last_nav = nav
        reconstructed.append(dict(date=day, nav=float(nav), wealth=float(wealth), cash=float(cash)))
    check(_canonical(trace["trades"]) == _canonical(fills), "flattened trade ledger differs from dated decisions")
    expected_ending = {symbol: float(quantity) for symbol, quantity in sorted(quantities.items())}
    check(set(trace["ending_positions"]) == set(expected_ending)
          and all(_close(trace["ending_positions"][symbol], quantity) for symbol, quantity in expected_ending.items()),
          "ending held quantities differ")
    check(_close(metrics["contributions"], total_deposits), "aggregate contributions differ")
    check(_close(metrics["total_execution_costs"], total_costs), "aggregate fees/rounding costs differ")
    check(_close(metrics["deployed_marked_principal"], total_principal), "aggregate marked purchases differ")
    check(_close(metrics["net_gain_after_costs"], _decimal(reconstructed[-1]["wealth"]) - initial - total_deposits),
          "net gain does not remove all external capital")
    check(metrics["trade_count"] == len(fills) and metrics["decision_count"] == len(decisions), "aggregate decision/fill counts differ")
    check(metrics["abstaining_decisions"] == abstentions and metrics["execution_rejections"] == rejections,
          "aggregate abstention/rejection counts differ")
    check(metrics["old_cash_reoffered"] is False, "metric claims old cash was reoffered")
    _check_metrics(reconstructed, metrics, 1.0, before_start, check, "full sample")
    development = [point for point in reconstructed if point["date"] <= parameters["development_end"]]
    evaluation = [point for point in reconstructed if point["date"] > parameters["development_end"]]
    _check_metrics(development, trace["development"], 1.0, before_start, check, "development")
    _check_metrics(evaluation, trace["evaluation"], development[-1]["nav"] if development else 1.0,
                   development[-1]["date"] if development else before_start, check, "evaluation")
    contract = trace["execution_contract"]
    check(contract["decision_scope"] == "new_contribution_only"
          and contract["existing_cash_reoffered"] is False
          and contract["sales"] is False and contract["leverage"] is False,
          "execution contract does not preserve contribution-only long-only scope")
    check(contract["initial_valuation_date"] == before_start, "initial valuation date differs")
    check(contract["execution_stages"] == ["exchange_close_17_30_Europe_Berlin", "crypto_close_23_59_59_UTC"]
          and contract["later_stage_may_change_earlier_fills"] is False
          and contract["risk_history_at_fill"] == "only_observations_through_previous_calendar_month_end"
          and contract["contribution_event"] == "00_00_UTC_on_execution_date_at_previous_completed_day_NAV",
          "execution contract permits future-event information or retrospective fills")
    checked["trials"] += 1


def _check_metrics(points, metrics, initial_nav, initial_date, check, label):
    check(metrics["observations"] == len(points), label + ": observation count differs")
    if not points:
        return
    peak, drawdown, underwater, longest = initial_nav, 0.0, 0, 0
    previous, daily = initial_nav, []
    for point in points:
        nav = point["nav"]
        daily.append(nav / previous - 1.0)
        previous = nav
        if nav >= peak - 1e-12:
            peak, underwater = max(peak, nav), 0
        else:
            underwater += 1
            longest = max(longest, underwater)
        drawdown = max(drawdown, 1.0 - nav / peak)
    sundays = [point for point in points if _date(point["date"]).weekday() == 6]
    weekly = [b["nav"] / a["nav"] - 1.0 for a, b in zip(sundays, sundays[1:])
              if (_date(b["date"]) - _date(a["date"])).days == 7]
    ratio = points[-1]["nav"] / initial_nav
    years = max(1 / 365.25, (_date(points[-1]["date"]) - _date(initial_date)).days / 365.25)
    expected = {
        "time_weighted_total_return_after_costs": ratio - 1.0,
        "time_weighted_cagr_after_costs": ratio ** (1.0 / years) - 1.0 if ratio > 0 else -1.0,
        "max_unitized_drawdown": drawdown,
        "ending_nominal_wealth": points[-1]["wealth"], "ending_cash": points[-1]["cash"],
        "ending_cash_fraction": points[-1]["cash"] / points[-1]["wealth"] if points[-1]["wealth"] else 1.0,
        "annualized_weekly_volatility": statistics.pstdev(weekly) * math.sqrt(52) if len(weekly) > 1 else None,
        "annualized_calendar_daily_volatility": statistics.pstdev(daily) * math.sqrt(365.25) if len(daily) > 1 else None,
        "empirical_weekly_es95_after_costs": _tail_loss(weekly),
    }
    for key, value in expected.items():
        check(metrics.get(key) is None if value is None else _close(metrics[key], value), label + ": " + key + " differs")
    check(metrics["weekly_observations"] == len(weekly), label + ": incomplete/missing week counted as a full week")
    check(metrics["longest_underwater_calendar_days"] == longest, label + ": underwater duration differs")


def _safe_child(directory, name):
    path = (directory / name).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("Artifact path escapes frozen study directory")
    return path


def _scenario_document(base, scenario):
    if scenario["dataset"] == "synthetic":
        # A deterministic source fixture, never a decision/accounting function.
        from research.chronological_validation import synthetic_document
        return synthetic_document(scenario["regime"])
    document = deepcopy(base)
    if "asset_categories" in scenario:
        categories = scenario["asset_categories"]
        document["assets"] = {symbol: asset for symbol, asset in document["assets"].items()
                              if asset["category"] in categories}
        document["study_subset_categories"] = list(categories)
    return document


def verify_run(run_dir: Path, data_path: Path):
    """Audit every frozen trial, source manifest, dataset and hash-chain record."""
    directory = Path(run_dir)
    result = json.loads((directory / "results.json").read_text())
    plan = json.loads((directory / "predeclared_plan.json").read_text())
    trials = json.loads((directory / "predeclared_trials.json").read_text())
    manifest = json.loads((directory / "source_manifest.json").read_text())
    base_bytes = Path(data_path).read_bytes()
    base = json.loads(base_bytes)
    problems, checked = [], defaultdict(int)

    def check(condition, message):
        if not condition:
            problems.append(message)

    check(_sha(base_bytes) == plan["dataset_sha256"], "base source snapshot differs from predeclared plan")
    check(_sha(_canonical(plan)) == manifest["plan_sha256"], "predeclared plan hash differs")
    check(_sha(_canonical(trials)) == manifest["trials_sha256"], "predeclared trials hash differs")
    check(_sha((directory / "source_manifest.json").read_bytes()) == result["source_manifest_sha256"],
          "source manifest hash differs from retained results")
    check(result["study_id"] == manifest["study_id"] == plan["study_id"], "study identities differ")
    check(manifest["baseline_commit"] == plan["baseline_commit"], "baseline commit differs from frozen plan")
    source_hashes = manifest["files"]
    check(bool(source_hashes), "frozen source manifest is empty")
    source_files = list((REPO / "custom_components/investment").rglob("*.py"))
    source_files += list((REPO / "research").glob("*.py"))
    source_files += list((REPO / "research/frozen_v13").glob("*.py"))
    source_files += list((REPO / "tests").glob("test_contribution*.py"))
    check(set(source_hashes) == {str(path.relative_to(REPO)) for path in source_files},
          "frozen source manifest omits or adds a source file")
    for filename, digest in source_hashes.items():
        try:
            path = _safe_child(REPO, filename)
            check(_sha(path.read_bytes()) == digest, "source file changed since freeze: " + filename)
            checked["source_files"] += 1
        except (OSError, ValueError) as error:
            problems.append("source file unavailable: " + filename + ": " + str(error))
    for filename, digest in manifest["datasets"].items():
        try:
            check(_sha(_safe_child(REPO, filename).read_bytes()) == digest, "raw source file changed: " + filename)
            checked["raw_source_files"] += 1
        except (OSError, ValueError) as error:
            problems.append("raw source file unavailable: " + filename + ": " + str(error))
    for symbol, asset in base["assets"].items():
        if not asset.get("source", {}).get("raw_snapshot"):
            continue
        try:
            payload = gzip.decompress(_safe_child(Path(data_path).parent, asset["source"]["raw_snapshot"]).read_bytes())
            check(_sha(payload) == asset["source"]["raw_sha256"], symbol + ": original provider payload hash differs")
            checked["provider_payloads"] += 1
        except (OSError, KeyError, TypeError, ValueError) as error:
            problems.append(symbol + ": provider payload verification failed: " + str(error))
    cases, ledger, previous = result["cases"], [], None
    for line in (directory / "experiment_ledger.jsonl").read_text().splitlines():
        row = json.loads(line)
        digest = row.pop("record_sha256")
        check(row.get("previous_record_sha256") == previous and _sha(_canonical(row)) == digest,
              "broken experiment hash chain at record " + str(len(ledger)))
        previous = digest
        ledger.append(row)
    check(len(ledger) == len(cases), "ledger/case count mismatch")
    check(result["ledger_last_sha256"] == previous, "results refer to a different ledger endpoint")
    check(result["trial_count"] == len(trials) == len(cases), "predeclared/result trial counts differ")
    check(result["completed_count"] == sum(case["status"] == "completed" for case in cases)
          and result["failed_count"] == sum(case["status"] != "completed" for case in cases),
          "completed/failed result counts disagree with retained trials")
    check(result["source_freeze_valid"] is True, "runner did not validate its source freeze")
    ledger_by_id = {row["trial_id"]: row for row in ledger}
    check(len(ledger_by_id) == len(ledger), "duplicate ledger trial identifier")
    expected = Counter((scenario["id"], strategy) for scenario in plan["scenarios"] for strategy in plan["strategies"])
    actual = Counter((case["scenario"], case["strategy"]) for case in cases)
    check(actual == expected, "missing, extra or duplicate predeclared strategy/scenario trials")
    check(len({case["trial_id"] for case in cases}) == len(cases), "duplicate result trial identifier")
    expected_pairs = [(scenario["id"], strategy) for scenario in plan["scenarios"] for strategy in plan["strategies"]]
    check([(case["scenario"], case["strategy"]) for case in trials] == expected_pairs,
          "frozen trial order differs from predeclared scenario/strategy order")
    check([case["trial_id"] for case in cases] == [case["trial_id"] for case in trials]
          == [row["trial_id"] for row in ledger], "retained trial ordering differs from the frozen schedule")
    for index, trial in enumerate(trials):
        check(trial["trial_id"] == f"{index + 1:03d}-{trial['scenario']}-{trial['strategy']}",
              "predeclared trial identifier differs from fixed trial order")
    scenarios = {scenario["id"]: scenario for scenario in plan["scenarios"]}
    documents, traces = {}, {}
    for scenario_id, scenario in scenarios.items():
        try:
            record = manifest["scenario_datasets"][scenario_id]
            raw = gzip.decompress(_safe_child(directory, record["file"]).read_bytes())
            document = json.loads(raw)
            check(_sha(raw) == record["sha256"] and _sha(_canonical(document)) == record["sha256"],
                  scenario_id + ": frozen dataset hash differs")
            check(_canonical(document) == _canonical(_scenario_document(base, scenario)),
                  scenario_id + ": source is not the declared dataset/subset/fixture")
            documents[scenario_id] = document
            checked["scenario_datasets"] += 1
        except (OSError, KeyError, TypeError, ValueError) as error:
            problems.append(scenario_id + ": dataset verification failed: " + str(error))
    for case in cases:
        trial_id = case["trial_id"]
        prefix = trial_id + ": "
        try:
            record = ledger_by_id.get(trial_id)
            check(record is not None, prefix + "missing ledger record")
            if record is not None:
                retained = {key: value for key, value in record.items() if key != "previous_record_sha256"}
                check(_canonical(retained) == _canonical(case), prefix + "ledger/result mismatch")
            declared = next((trial for trial in trials if trial["trial_id"] == trial_id), None)
            check(declared is not None, prefix + "undeclared trial")
            if declared is not None:
                check(all(_canonical(case.get(key)) == _canonical(value) for key, value in declared.items()),
                      prefix + "retained case differs from frozen trial")
            worker_path = _safe_child(directory, "attempts/" + trial_id + ".result.json")
            envelope = json.loads(worker_path.read_bytes())
            check(_sha(_canonical(envelope["record"])) == envelope["record_sha256"]
                  and _canonical(envelope["record"]) == _canonical(case),
                  prefix + "durable worker result differs from ledger/results")
            starts = sorted((directory / "attempts").glob(trial_id + ".*.started.json"))
            check(len(starts) == envelope["attempt_number"] and bool(starts),
                  prefix + "retained worker attempt count differs")
            previous_start = datetime.fromisoformat(manifest["frozen_at_utc"])
            check(previous_start.utcoffset() is not None, prefix + "freeze timestamp has no timezone")
            for number, path in enumerate(starts, 1):
                started = json.loads(path.read_bytes())
                instant = datetime.fromisoformat(started["started_at_utc"])
                check(path.name == f"{trial_id}.{number:03d}.started.json" and started["attempt_number"] == number
                      and _canonical(started["trial"]) == _canonical(declared),
                      prefix + "started attempt changed its declared parameters")
                check(instant.utcoffset() is not None and instant >= previous_start,
                      prefix + "worker attempt predates its source freeze or previous attempt")
                previous_start = instant
                checked["retained_worker_attempts"] += 1
            checked["durable_worker_results"] += 1
            if case["status"] != "completed":
                problems.append(prefix + "retained trial did not complete: " + str(case.get("error", "")))
                continue
            scenario = scenarios[case["scenario"]]
            parameters = {**plan["defaults"], **scenario.get("parameters", {}), "strategy": case["strategy"]}
            check(_canonical(parameters) == _canonical(case["parameters"]), prefix + "parameters differ from frozen plan")
            raw = gzip.decompress(_safe_child(directory, case["trace_file"]).read_bytes())
            check(_sha(raw) == case["trace_sha256"], prefix + "trace hash differs")
            trace = json.loads(raw)
            check(_canonical(trace["parameters"]) == _canonical(parameters), prefix + "trace parameters differ")
            document = documents[case["scenario"]]
            check(_sha(_canonical(document)) == case["dataset_sha256"], prefix + "trial source hash differs")
            for key, value in case["result"].items():
                check(_canonical(trace.get(key)) == _canonical(value), prefix + "summary differs from retained trace: " + key)
            check(set(case["result"]) == {"metrics", "development", "evaluation", "ending_positions", "execution_contract"},
                  prefix + "retained summary omits a required result")
            report = audit_trial(document, trace)
            for key, count in report["checked"].items():
                checked[key] += count
            problems.extend(prefix + violation for violation in report["violations"])
            if case["scenario"] in {"observed_primary", "prior_holdings_and_cash"}:
                traces[case["scenario"], case["strategy"]] = trace
        except (OSError, ArithmeticError, KeyError, TypeError, ValueError) as error:
            problems.append(prefix + "audit could not complete: " + type(error).__name__ + ": " + str(error))
    invariant_fields = ("decision_session", "execution_session", "calendar_assets", "contribution", "quote_budget",
                        "candidate_history", "orders", "trades", "execution_rejections", "quote_guard",
                        "fill_guard", "policy", "fill_cost_filters")
    for strategy in plan["strategies"]:
        baseline = traces.get(("observed_primary", strategy))
        changed = traces.get(("prior_holdings_and_cash", strategy))
        if baseline is None or changed is None:
            continue
        if baseline["parameters"]["existing_instruments"] != "allow":
            continue
        expected_trace = [{key: row[key] for key in invariant_fields} for row in baseline["trace"]]
        actual_trace = [{key: row[key] for key in invariant_fields} for row in changed["trace"]]
        check(_canonical(expected_trace) == _canonical(actual_trace), strategy + ": old cash/holdings changed contribution decisions")
        checked["old_account_invariance_pairs"] += 1
    check({path.name.removesuffix(".result.json") for path in (directory / "attempts").glob("*.result.json")}
          == {case["trial_id"] for case in cases}, "durable worker results include missing or undeclared trials")
    return {"schema": SCHEMA, "study_id": plan["study_id"], "passed": not problems,
            "checked": dict(checked), "violations": problems, "ledger_last_sha256": previous,
            "scope": "Frozen files/data/plan/trial identity and durable worker/hash-chain agreement; monthly and staged close chronology; completed-month trend and signal history; exact quote/fill authority, costs and contribution-only funding; lots and owned exclusion; independent historical plan risk for every committed stage prefix; holdings, midnight-flow-neutral NAV and period metrics; old-account invariance. No future-performance guarantee."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    report = verify_run(args.run_dir, args.data)
    output = args.run_dir / "independent_accounting_audit.json"
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps(report, allow_nan=False))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    main()
