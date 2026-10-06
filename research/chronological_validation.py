"""Chronological, contributions-only investment experiments with a retained ledger.

Research code, not a production recommender. All decisions use the completed
previous session. Currency-capped orders are filled at the next observed close;
the fill may be reduced for prices, lots and charges but is never increased.
Historical adjusted data are a current vintage, not point-in-time archival data.
No fitting, parameter search, short selling, leverage or sales take place here.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import gzip
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import types
from typing import Any, Callable


HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BASELINE_COMMIT = "f9c69933dbe79fdeeeb9fe78183095e184c2f30d"
BASELINE_HASHES = {
    "validated_model.py": "d7723f6fba2b6bcebd66eaa442d8ae5f5a9a22b23c699e9e061b1e6a3c6c4b05",
    "indication.py": "9788e1de8ac631299e0e584a8a34964b5c5825e5b140b5cf96ce8b240fffba8a",
    "instrument_identity.py": "634dd2b6aefd5ec928c24109ef7d993478b11e06de749aa6f0af64759bcd2b1e",
}
STRATEGIES = (
    "frozen_v13", "current", "strategic", "contribution_directed",
    "fixed_diagonal_shrinkage", "robust_windows", "smoothed_volatility_cap", "long_only_trend",
)
STRATEGIC = {
    "very_low": {"cash_like": .40, "aggregate_bond": .30, "broad_equity": .15},
    "low": {"cash_like": .25, "aggregate_bond": .35, "broad_equity": .30},
    "medium": {"cash_like": .15, "aggregate_bond": .30, "broad_equity": .50},
    "high": {"cash_like": .10, "aggregate_bond": .20, "broad_equity": .65},
    "very_high": {"cash_like": .05, "aggregate_bond": .10, "broad_equity": .80},
}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_stack(frozen: bool = False):
    path = HERE / "frozen_v13" if frozen else REPO / "custom_components" / "investment"
    name = "_investment_research_frozen" if frozen else "_investment_research_current"
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [str(path)]
        sys.modules[name] = package
    if frozen:
        for filename, expected in BASELINE_HASHES.items():
            if sha256((path / filename).read_bytes()) != expected:
                raise ValueError("Frozen comparison model changed")
    return importlib.import_module(name + ".validated_model"), importlib.import_module(name + ".indication")


def load_finalizer():
    load_stack(False)
    return importlib.import_module("_investment_research_current.portfolio_plan").finalize_purchase_plan


def _finite_positive(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def add_currency(a: float, b: float) -> float:
    """Preserve cent-valued cash flows before a later conservative floor.

    Binary ``102.27 + 100`` can become ``202.26999999999998``. Flooring that
    representation to cents silently loses money. This adds the recorded
    decimal amounts first; quantities and market valuations remain floats.
    """
    return float(Decimal(str(a)) + Decimal(str(b)))


class PricePanel:
    """Strict per-instrument availability; absent observations never become zero."""

    def __init__(self, document: dict[str, Any]):
        self.document = document
        self.assets = document["assets"]
        if not self.assets:
            raise ValueError("Empty research universe")
        self.dates: dict[str, list[str]] = {}
        self.bars: dict[str, list[dict[str, Any]]] = {}
        for symbol, asset in self.assets.items():
            inception = asset["inception"]
            if date.fromisoformat(inception).isoformat() != inception:
                raise ValueError("Invalid inception")
            rows = []
            previous = ""
            for raw in asset["bars"]:
                day = raw["date"]
                if date.fromisoformat(day).isoformat() != day or day <= previous:
                    raise ValueError("Prices must be unique and chronological")
                previous = day
                if not _finite_positive(raw["close"]) or not _finite_positive(raw["adjusted_close"]):
                    raise ValueError("Invalid price")
                if day >= inception:
                    rows.append(dict(raw))
            self.bars[symbol] = rows
            self.dates[symbol] = [row["date"] for row in rows]
        self.calendar = sorted(set().union(*(set(days) for days in self.dates.values())))

    def history(self, symbol: str, cutoff: str):
        return self.bars[symbol][:bisect_right(self.dates[symbol], cutoff)]

    def observed(self, symbol: str, day: str):
        rows = self.history(symbol, day)
        return rows[-1] if rows and rows[-1]["date"] == day else None

    def mark(self, symbol: str, day: str, *, stale_days=7):
        rows = self.history(symbol, day)
        if not rows or (date.fromisoformat(day) - date.fromisoformat(rows[-1]["date"])).days > stale_days:
            raise ValueError(f"Cannot value {symbol} at {day} without stale/missing prices")
        return rows[-1]["close"]


@dataclass(frozen=True)
class Costs:
    fixed_fee: float = 1.0
    commission_bps: float = 0.0
    execution_bps: float = 10.0

    def __post_init__(self):
        for value in asdict(self).values():
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError("Invalid explicit fee scenario")

    def charges(self, principal: Decimal) -> Decimal:
        if principal <= 0:
            return Decimal(0)
        fee = Decimal(str(self.fixed_fee)) + principal * Decimal(str(self.commission_bps + self.execution_bps)) / Decimal(10000)
        return fee.quantize(Decimal(".01"), rounding=ROUND_UP)

    def production_profile(self):
        # Kept beside the shared executor, so all comparisons disclose exactly
        # the same hypothetical charges. No broker schedule is inferred.
        return {
            "confirmed": True, "fixed_fee": self.fixed_fee,
            "commission_pct": self.commission_bps / 100, "spread_bps": self.execution_bps,
            "fx_bps": 0.0,
        }


@dataclass(frozen=True)
class Experiment:
    strategy: str = "current"
    risk: str = "medium"
    contribution: float = 100.0
    initial_cash: float = 0.0
    initial_positions: tuple[tuple[str, float], ...] = ()
    start: str = "2022-11-01"
    development_end: str = "2024-12-31"
    end: str = "2026-10-05"
    whole_units: bool = False
    max_drawdown_loss: float | None = .20
    shrinkage: float = .50
    trend_weeks: int = 40
    vol_smoothing: float = .25
    annual_inflation_assumption: float | None = None
    fixed_fee: float = 1.0
    commission_bps: float = 0.0
    execution_bps: float = 10.0

    def __post_init__(self):
        if self.strategy not in STRATEGIES or self.risk not in STRATEGIC:
            raise ValueError("Unsupported predeclared strategy or risk policy")
        if not _finite_positive(self.contribution) or isinstance(self.initial_cash, bool) or not math.isfinite(self.initial_cash) or self.initial_cash < 0:
            raise ValueError("Invalid contribution/cash")
        if any(not isinstance(symbol, str) or not symbol or isinstance(quantity, bool)
               or not math.isfinite(quantity) or quantity < 0 for symbol, quantity in self.initial_positions):
            raise ValueError("Invalid initial holding")
        if len({symbol for symbol, _ in self.initial_positions}) != len(self.initial_positions):
            raise ValueError("Duplicate initial holding")
        for day in (self.start, self.development_end, self.end):
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError("Invalid experiment date")
        if self.end < self.start:
            raise ValueError("Evaluation ends before it starts")
        if not 0 <= self.shrinkage <= 1 or not 0 < self.vol_smoothing <= 1 or self.trend_weeks < 2:
            raise ValueError("Invalid research challenger parameter")
        if self.annual_inflation_assumption is not None and self.annual_inflation_assumption <= -1:
            raise ValueError("Invalid inflation scenario")


def exact_es(returns: list[float], alpha: float = .95):
    """Independent loss-tail oracle, integrating the empirical quantile steps."""
    if not returns:
        return None
    if not 0 < alpha < 1 or any(isinstance(x, bool) or not math.isfinite(x) or x < -1 for x in returns):
        raise ValueError("Invalid ES observations")
    losses = sorted((-r for r in returns), reverse=True)
    mass = len(losses) * (1 - alpha)
    whole = int(math.floor(mass + 1e-12))
    fraction = max(0.0, mass - whole)
    total = sum(losses[:whole])
    if fraction > 1e-12:
        total += fraction * losses[whole]
    return max(0.0, total / mass)


def max_drawdown(values: list[float]):
    peak = 1.0
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = max(worst, 1 - value / peak)
    return worst


def aligned_matrix(rows):
    positive = [row for row in rows if row.get("risk_weekly_returns")]
    if not positive:
        return [], []
    keys = sorted(set.intersection(*(set(row["risk_weekly_returns"]) for row in positive)))
    return positive, [[row["risk_weekly_returns"][key] for row in positive] for key in keys]


def covariance(matrix: list[list[float]], shrinkage: float = 0.0):
    """Sample covariance shrunk toward its own diagonal with a FIXED lambda.

    This deliberately is not called Ledoit-Wolf: lambda is prespecified rather
    than estimated by that paper's optimal-shrinkage formula. Both component
    matrices are positive semidefinite, so their convex mixture is too.
    """
    if not 0 <= shrinkage <= 1 or len(matrix) < 2 or not matrix[0]:
        raise ValueError("Invalid covariance input")
    n, p = len(matrix), len(matrix[0])
    if any(len(row) != p or any(isinstance(x, bool) or not math.isfinite(x) for x in row) for row in matrix):
        raise ValueError("Invalid aligned observations")
    means = [statistics.fmean(row[j] for row in matrix) for j in range(p)]
    return [[
        sum((row[i] - means[i]) * (row[j] - means[j]) for row in matrix) / (n - 1) * (1 if i == j else 1 - shrinkage)
        for j in range(p)
    ] for i in range(p)]


def covariance_volatility(matrix, weights, shrinkage=0.0):
    cov = covariance(matrix, shrinkage)
    if len(weights) != len(cov):
        raise ValueError("Weight/matrix dimensions differ")
    variance = sum(weights[i] * cov[i][j] * weights[j] for i in range(len(weights)) for j in range(len(weights)))
    if variance < -1e-12:
        raise ArithmeticError("Covariance quadratic form became negative")
    return math.sqrt(max(0.0, variance) * 52)


def weekly_prices(history):
    grouped = {}
    for row in history:
        iso = date.fromisoformat(row["date"]).isocalendar()
        grouped[(iso.year, iso.week)] = row["adjusted_close"]
    return list(grouped.values())


def prepared_candidates(panel: PricePanel, cutoff: str, quantities: dict[str, float], cash: float, model, scorer):
    value_by_symbol = {symbol: quantity * panel.mark(symbol, cutoff) for symbol, quantity in quantities.items() if quantity > 0}
    total = sum(value_by_symbol.values()) + cash
    sleeve_values = defaultdict(float)
    for symbol, value in value_by_symbol.items():
        sleeve_values[panel.assets[symbol]["sleeve"]] += value
    candidates = []
    for symbol, asset in panel.assets.items():
        history = panel.history(symbol, cutoff)
        if not history or history[-1]["date"] != cutoff:
            continue
        risk_points = [{"ts": row["ts"], "value": row["adjusted_close"], "session_date": row["date"]} for row in history]
        risk_returns = model.weekly_return_map_from_points(risk_points, require_session_dates=True)
        # Medium horizon uses the same daily one-year scaffold as production.
        prices = [row["adjusted_close"] for row in history[-253:]]
        scale = history[-1]["close"] / prices[-1]
        prices = [price * scale for price in prices]
        scaffold = scorer.analyze_prices(
            prices, current_price=history[-1]["close"], category=asset["category"],
            holding_weight=value_by_symbol.get(symbol, 0) / total if total else 0,
            category_weight=sleeve_values[asset["sleeve"]] / total if total else 0,
            risk_tolerance=model.SIGNAL_SCAFFOLD_RISK, horizon="medium", strategy="adaptive", overlap_policy="allow",
        ).as_dict()
        metadata = {"provider": "yahoo" if panel.document["kind"] == "observed_adjusted_history" else "synthetic",
                    "provider_id": symbol, "symbol": symbol, "name": asset["name"],
                    "category": asset["category"], "currency": asset["currency"]}
        candidate = model.prepare_scored_candidate(scaffold, metadata, risk_returns)
        candidate.update({"portfolio_price": history[-1]["close"], "price": history[-1]["close"],
                          "last_observed_session": history[-1]["date"], "first_observed_session": history[0]["date"],
                          "research_weekly_prices": weekly_prices(history)})
        candidates.append(candidate)
    return candidates


def _positions(quantities, candidates, panel, cutoff):
    by_symbol = {row["symbol"]: row for row in candidates}
    result = []
    for symbol, quantity in quantities.items():
        if quantity <= 0:
            continue
        row = dict(by_symbol.get(symbol) or {})
        row.update({"symbol": symbol, "provider_id": symbol,
                    "provider": "yahoo" if panel.document["kind"] == "observed_adjusted_history" else "synthetic",
                    "quantity": quantity, "portfolio_price": panel.mark(symbol, cutoff),
                    "economic_sleeve": panel.assets[symbol]["sleeve"]})
        result.append(row)
    return result


def strategic_weights(candidates, risk):
    grouped = defaultdict(list)
    for row in candidates:
        if row.get("risk_history_eligible") and row.get("allocatable_economic_exposure"):
            grouped[row["economic_sleeve"]].append(row)
    return [(row, target / len(grouped[sleeve]))
            for sleeve, target in STRATEGIC[risk].items() for row in grouped[sleeve]]


def decide(panel, cutoff, quantities, cash, config, state, finalizer: Callable | None = None):
    frozen = config.strategy == "frozen_v13"
    model, scorer = load_stack(frozen)
    candidates = prepared_candidates(panel, cutoff, quantities, cash, model, scorer)
    budget = add_currency(cash, config.contribution)
    if config.strategy in {"strategic", "contribution_directed"}:
        weighted = strategic_weights(candidates, config.risk)
        if config.strategy == "contribution_directed":
            total = budget + sum(q * panel.mark(s, cutoff) for s, q in quantities.items())
            deficits = [(row, max(0.0, total * weight - quantities.get(row["symbol"], 0.0) * row["portfolio_price"])) for row, weight in weighted]
            desired = sum(value for _, value in deficits)
            weighted = [(row, value / max(budget, desired)) for row, value in deficits if value > 0]
        weighted, _ = model.scale_down_to_risk_contract(weighted, config.risk)
        # Baselines are policy/risk constrained, not retrospectively matched to
        # identical realised volatility. That difference is shown in outcomes.
        rows = [dict(row, suggested_amount=math.floor(weight * budget * 100 + 1e-9) / 100,
                     suggested_units=weight * budget / row["portfolio_price"], whole_units_only=config.whole_units)
                for row, weight in weighted]
    else:
        weighted, metadata = model.validated_exact_weights(candidates, config.risk)
        candidates = metadata["candidates"]
        weighted, _ = model.apply_downward_weight_constraints(weighted, risk=config.risk, min_confidence=.45)
        if config.strategy in {"fixed_diagonal_shrinkage", "robust_windows", "smoothed_volatility_cap"} and weighted:
            positive = [row for row, weight in weighted]
            aligned, matrix = aligned_matrix(positive)
            weight_by_symbol = {row["symbol"]: weight for row, weight in weighted}
            weights = [weight_by_symbol[row["symbol"]] for row in aligned]
            target = model.PORTFOLIO_VOL_TARGET[config.risk]
            factor = 1.0
            if len(matrix) >= 26:
                if config.strategy == "fixed_diagonal_shrinkage":
                    vol = covariance_volatility(matrix, weights, config.shrinkage)
                elif config.strategy == "robust_windows":
                    vol = max(covariance_volatility(matrix[-window:], weights) for window in (26, 52, 104, len(matrix)) if len(matrix) >= window)
                else:
                    vol = covariance_volatility(matrix[-26:], weights)
                factor = min(1.0, target / vol) if vol > 0 else 1.0
                if config.strategy == "smoothed_volatility_cap":
                    factor = min(1.0, config.vol_smoothing * factor + (1 - config.vol_smoothing) * state.get("previous_vol_factor", 1.0))
                    state["previous_vol_factor"] = factor
            weighted = [(row, weight * factor) for row, weight in weighted]
        elif config.strategy == "long_only_trend":
            weighted = [(row, weight) for row, weight in weighted
                        if row["economic_sleeve"] == "cash_like" or (
                            len(row.get("research_weekly_prices", [])) >= config.trend_weeks
                            and row["research_weekly_prices"][-1] >= statistics.fmean(row["research_weekly_prices"][-config.trend_weeks:]))]
        weighted, _ = model.scale_down_to_risk_contract(weighted, config.risk)
        rows, _ = model.production_projection(candidates, weighted, config.risk, budget, whole_units_only=config.whole_units)
    for row in rows:
        row["whole_units_only"] = config.whole_units
    guard_summary = None
    if not frozen:
        finalizer = finalizer or load_finalizer()
        rows, guard_summary = finalizer(
            rows, budget, config.risk,
            existing_positions=_positions(quantities, candidates, panel, cutoff), existing_cash=0.0,
            execution_costs=Costs(config.fixed_fee, config.commission_bps, config.execution_bps).production_profile(),
            max_drawdown_loss=config.max_drawdown_loss, portfolio_context="use", as_of=cutoff,
            bootstrap_repetitions=0,
        )
        guard_summary["research_budget_accounting"] = {
            "available_purchase_cash": budget,
            "carry_cash": cash,
            "new_external_contribution": config.contribution,
            "baseline_including_budget_is_complete_before_purchase": True,
        }
    return rows, candidates, guard_summary


def execute_currency_orders(rows, panel, session, quantities, cash, costs: Costs, whole_units=False):
    """Independent cash/lot executor, no borrow, no synthetic fills or selling."""
    balance = Decimal(str(cash)).quantize(Decimal(".01"), rounding=ROUND_DOWN)
    trades, rejected = [], []
    for row in sorted(rows, key=lambda x: (-float(x.get("suggested_amount") or 0), x["symbol"])):
        amount = Decimal(str(max(0.0, float(row.get("suggested_amount") or 0))))
        if amount <= 0:
            continue
        symbol = row["symbol"]
        bar = panel.observed(symbol, session)
        if bar is None:
            rejected.append({"symbol": symbol, "reason": "no_execution_observation"})
            continue
        price = Decimal(str(bar["close"]))
        rate = Decimal(str(costs.commission_bps + costs.execution_bps)) / Decimal(10000)
        affordable = max(Decimal(0), (balance - Decimal(str(costs.fixed_fee))) / (1 + rate))
        principal_limit = min(amount, affordable).quantize(Decimal(".01"), rounding=ROUND_DOWN)
        units = principal_limit / price
        units = units.quantize(Decimal(1) if whole_units else Decimal(".00000001"), rounding=ROUND_DOWN)
        if units <= 0:
            rejected.append({"symbol": symbol, "reason": "fees_or_lot_unaffordable"})
            continue
        # Conservatively round the exact principal UP for cash debits. The
        # holding mark uses actual units * observed price; any subcent remainder
        # is an explicit rounding cost, not secretly investable cash.
        principal = (units * price).quantize(Decimal(".01"), rounding=ROUND_UP)
        fee = costs.charges(principal)
        while units > 0 and (principal + fee > balance or principal > amount):
            # Reduce a monetary cent at a time, then recompute whole/fractional
            # units. Subtracting 1e-8 units can require millions of iterations
            # for low-priced assets merely to move the cash debit one cent.
            principal_limit = max(Decimal(0), principal_limit - Decimal(".01"))
            units = (principal_limit / price).quantize(Decimal(1) if whole_units else Decimal(".00000001"), rounding=ROUND_DOWN)
            principal = (units * price).quantize(Decimal(".01"), rounding=ROUND_UP)
            fee = costs.charges(principal)
        if units <= 0:
            rejected.append({"symbol": symbol, "reason": "fees_or_lot_unaffordable"})
            continue
        balance -= principal + fee
        quantities[symbol] = quantities.get(symbol, 0.0) + float(units)
        rounding_cost = principal - units * price
        trades.append({"symbol": symbol, "session": session, "units": float(units), "price": float(price),
                       "principal": float(principal), "fees": float(fee), "rounding_cost": float(rounding_cost),
                       "cash_debit": float(principal + fee)})
    if balance < 0:
        raise AssertionError("The execution oracle spent nonexistent cash")
    return float(balance), trades, rejected


def unitized_metrics(points, beginning_nav=1.0):
    if not points:
        return {"observations": 0}
    navs = [beginning_nav] + [p["nav"] for p in points]
    returns = [b / a - 1 for a, b in zip(navs, navs[1:]) if a > 0]
    normalized = [value / beginning_nav for value in navs]
    weeks = {}
    for point in points:
        iso = date.fromisoformat(point["date"]).isocalendar()
        weeks[(iso.year, iso.week)] = point["nav"]
    weekly = []
    previous = None
    for key, nav in weeks.items():
        monday = date.fromisocalendar(*key, 1)
        if previous and (monday - previous[0]).days == 7:
            weekly.append(nav / previous[1] - 1)
        previous = monday, nav
    peak = normalized[0]
    under, maximum_under = 0, 0
    for nav in normalized[1:]:
        if nav >= peak - 1e-12:
            peak, under = max(peak, nav), 0
        else:
            under += 1
            maximum_under = max(maximum_under, under)
    return {
        "observations": len(points), "first_session": points[0]["date"], "last_session": points[-1]["date"],
        "time_weighted_total_return_after_costs": navs[-1] / beginning_nav - 1,
        "annualized_daily_volatility": statistics.pstdev(returns) * math.sqrt(252) if len(returns) > 1 else 0,
        "max_unitized_drawdown": max_drawdown(normalized), "longest_underwater_observed_sessions": maximum_under,
        "empirical_weekly_es95_after_costs": exact_es(weekly), "weekly_observations": len(weekly),
        "ending_nominal_wealth": points[-1]["wealth"], "ending_cash": points[-1]["cash"],
        "ending_cash_fraction": points[-1]["cash"] / points[-1]["wealth"] if points[-1]["wealth"] else 1,
    }


def replay(document, config: Experiment, *, finalizer=None, capture_trace=True):
    panel = PricePanel(document)
    calendar = [day for day in panel.calendar if config.start <= day <= config.end]
    if not calendar:
        raise ValueError("No replay dates")
    quantities = dict(config.initial_positions)
    first_index = panel.calendar.index(calendar[0])
    if first_index == 0:
        raise ValueError("An earlier completed decision session is required")
    prior = panel.calendar[first_index - 1]
    cash = config.initial_cash
    initial_capital = cash + sum(q * panel.mark(s, prior) for s, q in quantities.items())
    issued_units = initial_capital
    nav = 1.0
    state = {}
    trace, points, trades_all = [], [], []
    contribution_total = 0.0
    decisions = abstentions = execution_rejections = historical_breaches = 0
    last_month = None
    costs = Costs(config.fixed_fee, config.commission_bps, config.execution_bps)
    risk_model, _ = load_stack(False)
    for session in calendar:
        index = panel.calendar.index(session)
        cutoff = panel.calendar[index - 1]
        month = session[:7]
        contribution = config.contribution if month != last_month else 0.0
        last_month = month
        before_flow = cash + sum(q * panel.mark(s, session) for s, q in quantities.items())
        if issued_units > 0:
            nav = before_flow / issued_units
        if contribution:
            orders, candidates, guard = decide(panel, cutoff, quantities, cash, config, state, finalizer)
            decisions += 1
            if any(row["last_observed_session"] > cutoff for row in candidates):
                raise AssertionError("Future price entered a decision")
            issued_units += contribution / nav
            cash = add_currency(cash, contribution)
            contribution_total += contribution
            cash, trades, rejected = execute_currency_orders(orders, panel, session, quantities, cash, costs, config.whole_units)
            trades_all.extend(trades)
            execution_rejections += len(rejected)
            abstentions += not bool(trades)
            post_total = cash + sum(q * panel.mark(s, session) for s, q in quantities.items())
            by_symbol = {row["symbol"]: row for row in candidates}
            weighted_post = [(by_symbol.get(s, {"risk_weekly_returns": {}}), q * panel.mark(s, session) / post_total)
                             for s, q in quantities.items() if q > 0]
            signature = risk_model.risk_signature(weighted_post)
            historical_ok = not weighted_post or risk_model.within_risk_target(signature, config.risk)
            historical_breaches += not historical_ok
            entry = {"decision_session": cutoff, "execution_session": session, "contribution": contribution,
                     "candidate_history": [{"symbol": row["symbol"], "first": row["first_observed_session"],
                         "last": row["last_observed_session"], "weekly_count": row["risk_history_weeks"],
                         "activation": row["activation"]} for row in candidates],
                     "orders": [{"symbol": row["symbol"], "amount": row.get("suggested_amount", 0)} for row in orders],
                     "trades": trades, "execution_rejections": rejected,
                     "post_historical_vol_es_check_passed": historical_ok,
                     "post_historical_signature": signature,
                     "guard": guard if capture_trace else None}
            trace.append(entry)
        wealth = cash + sum(q * panel.mark(s, session) for s, q in quantities.items())
        nav = wealth / issued_units if issued_units else 1.0
        points.append({"date": session, "wealth": wealth, "cash": cash, "nav": nav, "contribution": contribution})
    development = [p for p in points if p["date"] <= config.development_end]
    evaluation = [p for p in points if p["date"] > config.development_end]
    evaluation_start_nav = development[-1]["nav"] if development else 1.0
    metrics = unitized_metrics(points)
    years = max(1 / 365.25, (date.fromisoformat(calendar[-1]) - date.fromisoformat(calendar[0])).days / 365.25)
    fees = sum(t["fees"] + t["rounding_cost"] for t in trades_all)
    contributed = initial_capital + contribution_total
    metrics.update({"initial_capital": initial_capital, "contributions": contribution_total,
                    "net_gain_after_costs": metrics["ending_nominal_wealth"] - contributed,
                    "total_execution_costs": fees, "costs_fraction_of_capital_supplied": fees / contributed if contributed else 0,
                    "trade_count": len(trades_all), "decision_count": decisions, "abstaining_decisions": abstentions,
                    "execution_rejections": execution_rejections,
                    "decision_time_historical_vol_es_breaches": historical_breaches,
                    "purchase_turnover_per_year": sum(t["principal"] for t in trades_all) / statistics.fmean(p["wealth"] for p in points) / years,
                    "ending_real_wealth_in_starting_currency": None if config.annual_inflation_assumption is None else metrics["ending_nominal_wealth"] / (1 + config.annual_inflation_assumption) ** years})
    # One shared retrospective diagnostic at the final observed date. This does
    # not change orders or treat resampling quantiles as future probabilities.
    current_model, current_scorer = load_stack(False)
    ending_candidates = prepared_candidates(panel, calendar[-1], quantities, cash, current_model, current_scorer)
    ending_by_symbol = {row["symbol"]: row for row in ending_candidates}
    ending_weighted = [(ending_by_symbol.get(symbol, {"symbol": symbol}), q * panel.mark(symbol, calendar[-1]) / metrics["ending_nominal_wealth"])
                       for symbol, q in quantities.items() if q > 0]
    evidence_module = importlib.import_module("_investment_research_current.risk_evidence")
    ending_evidence = evidence_module.evaluate_risk_evidence(
        ending_weighted, as_of=calendar[-1], max_drawdown_loss=config.max_drawdown_loss,
        bootstrap_repetitions=128, bootstrap_seed=1729, bootstrap_block_weeks=8,
    )
    return {"parameters": asdict(config), "metrics": metrics,
            "development": unitized_metrics(development), "evaluation": unitized_metrics(evaluation, evaluation_start_nav),
            "trace": trace if capture_trace else [], "daily_accounting": points if capture_trace else [],
            "ending_positions": quantities, "ending_conditional_risk_evidence": ending_evidence}


def synthetic_document(regime="persistent_decline", sessions=1045):
    """Deterministic adversarial fixtures, explicitly not a forecast or evidence of alpha."""
    allowed = {"persistent_decline", "v_rebound", "joint_stock_bond_loss", "flat_cost_drag", "late_inception"}
    if regime not in allowed:
        raise ValueError("Unknown synthetic regime")
    symbols = {
        "CASH": ("Synthetic EUR Overnight Money Market", "cash_like", 100.0),
        "BOND": ("Synthetic Global Aggregate Bond", "aggregate_bond", 80.0),
        "EQUITY": ("Synthetic MSCI World Broad Equity", "broad_equity", 120.0),
    }
    dates, day = [], date(2020, 1, 2)
    while len(dates) < sessions:
        if day.weekday() < 5:
            dates.append(day.isoformat())
        day += timedelta(days=1)
    assets = {}
    for symbol, (name, sleeve, value) in symbols.items():
        bars = []
        for i, day in enumerate(dates):
            cash_ret = .00005
            bond_ret = .00003 + .0008 * math.sin(i * .37)
            equity_ret = .00020 + .003 * math.sin(i * .31)
            if i >= 520:
                j = i - 520
                if regime == "persistent_decline":
                    equity_ret = -.002
                elif regime == "v_rebound":
                    equity_ret = -.008 if j < 100 else .010 if j < 200 else .0002
                elif regime == "joint_stock_bond_loss":
                    equity_ret = -.003 if j < 180 else .001
                    bond_ret = -.0015 if j < 180 else .0005
                elif regime == "flat_cost_drag":
                    equity_ret = bond_ret = cash_ret = 0.0
            value *= 1 + {"CASH": cash_ret, "BOND": bond_ret, "EQUITY": equity_ret}[symbol]
            stamp = int(datetime.combine(date.fromisoformat(day), datetime.min.time(), timezone.utc).timestamp()) + 12 * 3600
            bars.append({"date": day, "ts": stamp, "close": value, "adjusted_close": value})
        inception = dates[720] if regime == "late_inception" and symbol == "EQUITY" else dates[0]
        assets[symbol] = {"name": name, "sleeve": sleeve, "inception": inception, "category": "etf", "currency": "EUR", "bars": bars}
    return {"schema": "ha-investment-research-source-v1", "kind": "synthetic_adversarial_scenario",
            "regime": regime, "currency": "EUR", "assets": assets,
            "limitations": ["Deliberately constructed stress path; no statistical likelihood or forecast is assigned."]}


def source_fingerprint():
    files = [HERE / "chronological_validation.py", HERE / "experiment_plan.json"]
    files += sorted((HERE / "frozen_v13").glob("*.py"))
    for filename in ("validated_model.py", "indication.py", "portfolio_plan.py", "risk_evidence.py", "execution_costs.py"):
        path = REPO / "custom_components" / "investment" / filename
        if path.exists():
            files.append(path)
    return {str(path.relative_to(REPO)): sha256(path.read_bytes()) for path in files}


def write_ledger(path: Path, records):
    """Write-once hash chain. Existing experiments are never overwritten."""
    previous = None
    with path.open("x", encoding="utf-8") as stream:
        for record in records:
            payload = {**record, "previous_record_sha256": previous}
            digest = sha256(canonical(payload))
            stream.write(canonical({**payload, "record_sha256": digest}).decode() + "\n")
            previous = digest
        stream.flush()
        os.fsync(stream.fileno())
    return previous


def verify_ledger(path: Path):
    previous = None
    count = 0
    for line in path.read_text().splitlines():
        row = json.loads(line)
        digest = row.pop("record_sha256")
        if row.get("previous_record_sha256") != previous or sha256(canonical(row)) != digest:
            raise ValueError(f"Experiment ledger hash chain is broken at record {count}")
        previous = digest
        count += 1
    return count, previous


def append_ledger_record(path: Path, record, previous):
    """Append one completed or failed attempt before the next trial begins."""
    before_count, endpoint = verify_ledger(path)
    if endpoint != previous:
        raise ValueError("Unexpected ledger endpoint before append")
    payload = {**record, "previous_record_sha256": previous}
    digest = sha256(canonical(payload))
    with path.open("a", encoding="utf-8") as stream:
        stream.write(canonical({**payload, "record_sha256": digest}).decode() + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    after_count, endpoint = verify_ledger(path)
    if after_count != before_count + 1 or endpoint != digest:
        raise ValueError("Appended trial was not durably retained in the ledger")
    return digest


def experiment_suite(plan, dataset):
    base = plan["defaults"]
    for risk in plan["primary_risks"]:
        for strategy in STRATEGIES:
            yield "observed_primary", dataset, Experiment(**{**base, "risk": risk, "strategy": strategy})
    for variant in plan["sensitivity_scenarios"]:
        for strategy in variant["strategies"]:
            settings = {k: v for k, v in variant.items() if k not in {"label", "strategies"}}
            yield variant["label"], dataset, Experiment(**{**base, **settings, "strategy": strategy})
    for regime in plan["synthetic_regimes"]:
        synthetic = synthetic_document(regime)
        for strategy in STRATEGIES:
            yield "synthetic_" + regime, synthetic, Experiment(**{**base, "strategy": strategy,
                      "start": "2022-01-03", "development_end": "2022-12-30", "end": "2023-12-29"})


def render_report(results, meta):
    successful = [r for r in results if r["status"] == "completed"]
    failed = [r for r in results if r["status"] != "completed"]
    lines = ["# Chronological investment experiment", "", f"Run: `{meta['run_id']}`. Baseline commit: `{BASELINE_COMMIT}`.", "",
        f"Completed {len(successful)} prespecified trials; retained {len(failed)} failed trials.", "",
        "This is an engineering and retrospective comparison on a current surviving universe. The later period is a chronological evaluation partition of known history, not an untouched prospective holdout. No alternative is automatically promoted to production.", "",
        "## Primary observed-data comparison", "",
        "All entries use the same explicit contribution, price timing, cost and lot scenario. Risk profiles share policy limits; realised risk is measured, not forced to match retrospectively.", "",
        "| Risk | Strategy | Ending wealth (€) | Cost (€) | Unitized drawdown | Annualized volatility | Ending cash | No-purchase decisions |", "|---|---|---:|---:|---:|---:|---:|---:|"]
    if meta.get("source_freeze_valid") is False:
        lines[2:2] = ["**INVALIDATED: source changed during this run. Retained for audit only; do not use as final-source results.**", ""]
    for r in successful:
        if r["label"] != "observed_primary":
            continue
        m, p = r["result"]["metrics"], r["parameters"]
        lines.append(f"| {p['risk']} | {p['strategy']} | {m['ending_nominal_wealth']:.2f} | {m['total_execution_costs']:.2f} | {m['max_unitized_drawdown']:.2%} | {m['annualized_daily_volatility']:.2%} | {m['ending_cash_fraction']:.2%} | {m['abstaining_decisions']}/{m['decision_count']} |")
    lines += ["", "## Later chronological evaluation partition", "", "Performance below is measured from the boundary NAV, with each strategy's existing holdings carried forward. Ending wealth includes earlier contributions and is therefore omitted from this period-return table.", "",
              "| Risk | Strategy | Time-weighted return after costs | Unitized drawdown | Annualized volatility |", "|---|---|---:|---:|---:|"]
    for r in successful:
        if r["label"] != "observed_primary":
            continue
        m, p = r["result"]["evaluation"], r["parameters"]
        if m.get("observations"):
            lines.append(f"| {p['risk']} | {p['strategy']} | {m['time_weighted_total_return_after_costs']:.2%} | {m['max_unitized_drawdown']:.2%} | {m['annualized_daily_volatility']:.2%} |")
    lines += ["", "## Scope and interpretation", "",
        "- Each month uses the previous completed session's observations; currency-capped orders execute at the following observed close. No future price is used to choose weights.",
        "- Existing holdings are carried forward and never sold. Contributions and leftover cash are available for subsequent decisions.",
        "- Costs are explicit illustrative scenarios, not a broker quote. Shares, cash, fees and subcent rounding are accounted for independently of the production planner.",
        "- Deposits issue portfolio units at the pre-deposit NAV; performance and drawdown use unitized NAV so deposits cannot hide investment losses.",
        "- The frozen model is the exact retained allocation and signal source from the baseline commit. The current model additionally uses the new complete-portfolio finalizer. This is a model/allocator replay, not an end-to-end Home Assistant/provider/authentication replay.",
        "- The declared 20% cumulative-loss policy is an illustrative input, not an empirically calibrated default or a maximum possible future loss.",
        "- The simple baselines obey the same named risk policy and current full-portfolio guard. They do not have identical realised volatility, which is reported separately.",
        "- Fixed diagonal covariance shrinkage uses a prespecified lambda; it does not implement or claim Ledoit-Wolf optimal intensity. All challengers only reduce suggested purchases and undergo the same final guard.",
        "- The zero-fee, contribution-size, whole-lot, drawdown-policy and parameter sensitivity trials are retained in JSON alongside synthetic persistent decline, reversal, joint equity/bond loss, flat returns and late-inception cases.",
        "- Synthetic scenarios are deliberately constructed stress paths without assigned probabilities. They do not establish expected investment performance.",
        "- Current adjusted data can contain historical revisions. The universe contains today's surviving verified funds, excludes delisted products and has no point-in-time membership reconstruction.",
        "- Uninvested cash earns zero in this experiment. Taxes, historical spreads, actual broker fee schedules and cash interest are not reconstructed. Real wealth is only computed when an explicitly labelled inflation assumption is supplied.",
        "- Observed dates cover the source snapshot only; the experiment cannot validate historical crises outside those dates or guarantee future returns.",
        "- Every attempted trial is retained in a write-once hash-chained ledger with source, data, configuration and code fingerprints. Reusing a run ID is refused.", ""]
    if failed:
        lines += ["## Retained failures", ""]
        for r in failed:
            lines.append(f"- {r['trial_id']}: {r['error']}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", type=Path, default=HERE / "results")
    args = parser.parse_args()
    if not args.run_id.replace("-", "").replace("_", "").isalnum():
        raise SystemExit("Run ID must contain only letters, digits, hyphens and underscores")
    directory = args.output_dir / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    data_bytes = args.data.read_bytes()
    dataset = json.loads(data_bytes)
    plan_bytes = (HERE / "experiment_plan.json").read_bytes()
    plan = json.loads(plan_bytes)
    fingerprint = source_fingerprint()
    meta = {"run_id": args.run_id, "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "baseline_commit": BASELINE_COMMIT, "plan_sha256": sha256(plan_bytes),
            "data_sha256": sha256(data_bytes), "data_file": str(args.data), "source_fingerprint": fingerprint,
            "parameters_fitted": False, "selection_based_on_results": False, "promotion": "none_research_only"}
    meta["repository_head_at_run"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    meta["code_identity"] = "repository base commit plus exact content fingerprints; the current working source may not yet be committed"
    # Record the exact plan before any outcomes are computed.
    (directory / "predeclared_plan.json").write_bytes(plan_bytes)
    (directory / "provenance.json").write_bytes(canonical(meta) + b"\n")
    ledger_path = directory / "experiment_ledger.jsonl"
    write_ledger(ledger_path, [])
    last_digest = None
    results = []
    for index, (label, document, config) in enumerate(experiment_suite(plan, dataset), 1):
        trial_id = f"{index:03d}-{label}-{config.risk}-{config.strategy}"
        record = {"trial_id": trial_id, "label": label, "parameters": asdict(config),
                  "data_sha256": sha256(canonical(document)), "code_fingerprint_sha256": sha256(canonical(fingerprint))}
        try:
            result = replay(document, config, capture_trace=True)
            trace_bytes = canonical({"trace": result.pop("trace"), "daily_accounting": result.pop("daily_accounting")})
            trace_name = trial_id + ".json.gz"
            (directory / trace_name).write_bytes(gzip.compress(trace_bytes, mtime=0))
            record.update(status="completed", result=result, trace_file=trace_name, trace_sha256=sha256(trace_bytes))
            m = result["metrics"]
            print(f"{trial_id}: wealth={m['ending_nominal_wealth']:.2f} drawdown={m['max_unitized_drawdown']:.2%} trades={m['trade_count']}", flush=True)
        except Exception as error:
            record.update(status="failed", error=f"{type(error).__name__}: {error}")
            print(f"{trial_id}: FAILED {record['error']}", flush=True)
        results.append(record)
        last_digest = append_ledger_record(ledger_path, record, last_digest)
    meta["source_freeze_valid"] = source_fingerprint() == fingerprint
    if not meta["source_freeze_valid"]:
        meta["invalidation_reason"] = "Source changed during the experiment; retained outcomes must not be used as a final-source comparison. Rerun under a new ID after code stabilizes."
    meta["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    meta["ledger_last_sha256"] = last_digest
    ledger_count, ledger_endpoint = verify_ledger(ledger_path)
    meta["ledger_verified_complete"] = ledger_count == len(results) and ledger_endpoint == last_digest
    (directory / "results.json").write_bytes(canonical({"provenance": meta, "results": results}) + b"\n")
    (directory / "REPORT.md").write_text(render_report(results, meta), encoding="utf-8")
    print(f"report={directory / 'REPORT.md'} completed={sum(r['status'] == 'completed' for r in results)} failed={sum(r['status'] != 'completed' for r in results)}", flush=True)
    if not meta["source_freeze_valid"] or not meta["ledger_verified_complete"] or any(r["status"] != "completed" for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
