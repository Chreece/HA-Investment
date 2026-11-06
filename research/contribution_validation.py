"""Deterministic replay of the current new-contribution purchase contract.

Research only.  Every monthly decision can spend its new external contribution
once.  Old cash and positions are recorded for performance, never supplied to
the signal, allocator or risk gate.  An optional exact-symbol ownership filter
is the sole exception for existing quantities.  No sale, short or borrowing is
possible.  Signals use the previous completed calendar month; execution uses a
strictly later common observed session, with literal quantity AND principal
ceilings.  New prices may reduce a purchase but can never increase its units.

This is a replay of the pure production scoring/allocation/execution kernels,
not a reconstruction of historical Home Assistant state, source vintages,
broker permissions, identifier registries or the live asynchronous provider
path.  Those distinctions are retained in the machine-readable contract.
"""
from __future__ import annotations

from bisect import bisect_right
from calendar import monthrange
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, localcontext
import importlib
import math
import statistics
from typing import Any
from zoneinfo import ZoneInfo

from research.chronological_validation import exact_es, load_stack
from research.contribution_candidates import (
    STRATEGIES, filter_approved_rows, initial_weights, transform_candidates,
)


BASELINE_COMMIT = "99c0e36cadd27439c1e2e78418bc601410f5547d"
SCHEMA = "ha-investment-contribution-replay-v1"
CENT = Decimal("0.01")
ZERO = Decimal(0)
DAY_SECONDS = 86400


def _number(value: Any, name: str, *, minimum: float = 0.0, maximum: float = 1e15) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{name} is outside its permitted range")
    return result


def _day(value: Any) -> date:
    if not isinstance(value, str):
        raise ValueError("Dates must be canonical ISO strings")
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError("Dates must be canonical ISO strings")
    return result


def _cash(value: Any, name: str) -> Decimal:
    _number(value, name)
    result = Decimal(str(value))
    if result != result.quantize(CENT):
        raise ValueError(f"{name} must be an exact number of currency cents")
    return result


def _end_stamp(day: str) -> int:
    return int(datetime.combine(_day(day), datetime.min.time(), timezone.utc).timestamp()) + DAY_SECONDS - 1


def _prior_year(day: date) -> date:
    # A provider's daily one-year history is a calendar window, not 253 prices.
    # Leap-day decisions use the previous February's final day.
    return date(day.year - 1, day.month, min(day.day, monthrange(day.year - 1, day.month)[1]))


def _month_start_after(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def _core_module(name: str):
    model, _ = load_stack(False)
    return importlib.import_module(model.__name__.rpartition(".")[0] + "." + name)


@dataclass(frozen=True)
class Costs:
    """Explicit hypothetical per-order charges in the panel's currency."""

    fixed_fee: float = 1.0
    commission_bps: float = 0.0
    execution_bps: float = 10.0
    fx_bps: float = 0.0

    def __post_init__(self):
        for key, value in asdict(self).items():
            _number(value, key, maximum=1e9 if key == "fixed_fee" else 10000)

    def production_profile(self):
        return {
            "confirmed": True, "fixed_fee": self.fixed_fee,
            "commission_pct": self.commission_bps / 100.0,
            "spread_bps": self.execution_bps, "fx_bps": self.fx_bps,
        }

    def exact_charge(self, principal: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        """Independent Decimal accounting, without production cost helpers.

        Fees are charged on marked principal before one conservative rounding
        of the complete cash debit.  Rounding is shown separately from fees.
        """
        if principal <= 0:
            return ZERO, ZERO, ZERO
        rate = sum((Decimal(str(x)) for x in
                    (self.commission_bps, self.execution_bps, self.fx_bps)), ZERO) / 10000
        fee = Decimal(str(self.fixed_fee)) + principal * rate
        debit = (principal + fee).quantize(CENT, rounding=ROUND_CEILING)
        return fee, debit - principal - fee, debit


@dataclass(frozen=True)
class Experiment:
    strategy: str = "baseline_current"
    risk: str = "medium"
    contribution: float = 100.0
    start: str = "2020-08-03"
    development_end: str = "2023-12-31"
    end: str = "2026-11-05"
    initial_cash: float = 0.0
    initial_positions: tuple[tuple[str, float], ...] = ()
    whole_units_only: bool = False
    whole_unit_categories: tuple[str, ...] = ()
    existing_instruments: str = "allow"
    max_candidate_fraction: float = .45
    max_candidate_fraction_is_hard: bool = False
    min_confidence: float = .45
    minimum_cash_reserve_fraction: float = 0.0
    max_drawdown_loss: float | None = None
    fixed_fee: float = 1.0
    commission_bps: float = 0.0
    execution_bps: float = 10.0
    fx_bps: float = 0.0

    def __post_init__(self):
        if self.strategy not in STRATEGIES:
            raise ValueError("Unsupported predeclared research strategy")
        if self.risk not in {"very_low", "low", "medium", "high", "very_high"}:
            raise ValueError("Unsupported risk profile")
        if self.existing_instruments not in {"allow", "exclude"}:
            raise ValueError("Unsupported ownership filter")
        if not isinstance(self.whole_units_only, bool) or not isinstance(self.max_candidate_fraction_is_hard, bool):
            raise ValueError("Lot and cap switches must be booleans")
        if not isinstance(self.whole_unit_categories, (list, tuple)):
            raise ValueError("Whole-unit categories must be an explicit sequence")
        allowed_categories = {"crypto", "etf", "stock", "fund", "index", "commodity", "fx", "other"}
        if (any(item not in allowed_categories for item in self.whole_unit_categories)
                or len(set(self.whole_unit_categories)) != len(self.whole_unit_categories)):
            raise ValueError("Invalid whole-unit category")
        object.__setattr__(self, "whole_unit_categories", tuple(self.whole_unit_categories))
        if _cash(self.contribution, "contribution") <= 0:
            raise ValueError("Contribution must be positive")
        _cash(self.initial_cash, "initial_cash")
        for field in ("max_candidate_fraction", "min_confidence", "minimum_cash_reserve_fraction"):
            _number(getattr(self, field), field, maximum=1.0)
        if self.max_candidate_fraction <= 0:
            raise ValueError("Candidate cap must be positive")
        if self.max_drawdown_loss is not None:
            _number(self.max_drawdown_loss, "max_drawdown_loss", minimum=1e-15, maximum=1.0)
        if _day(self.end) < _day(self.start):
            raise ValueError("Evaluation ends before it starts")
        _day(self.development_end)
        if not isinstance(self.initial_positions, (list, tuple)):
            raise ValueError("Initial positions must be explicit symbol/quantity pairs")
        identities = set()
        for pair in self.initial_positions:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError("Invalid initial position")
            symbol, quantity = pair
            if not isinstance(symbol, str) or not symbol or symbol in identities:
                raise ValueError("Duplicate or invalid initial symbol")
            identities.add(symbol)
            _number(quantity, "initial_quantity", maximum=1e24)
        object.__setattr__(self, "initial_positions", tuple(tuple(pair) for pair in self.initial_positions))
        Costs(self.fixed_fee, self.commission_bps, self.execution_bps, self.fx_bps)

    def costs(self) -> Costs:
        return Costs(self.fixed_fee, self.commission_bps, self.execution_bps, self.fx_bps)


class PricePanel:
    """One fixed-currency source snapshot with strict chronological availability."""

    def __init__(self, document: dict[str, Any]):
        if not isinstance(document, dict) or not isinstance(document.get("assets"), dict) or not document["assets"]:
            raise ValueError("A nonempty research universe is required")
        # A caller may reuse its parsed JSON for another scenario.  Neither
        # that caller nor a candidate transformation may change this panel's
        # retained observation snapshot through a shared nested object.
        self.document = deepcopy(document)
        self.assets = self.document["assets"]
        self.currency = str(document.get("currency") or "EUR")
        self.dates: dict[str, list[str]] = {}
        self.bars: dict[str, list[dict[str, Any]]] = {}
        for symbol, asset in self.assets.items():
            if not isinstance(symbol, str) or not symbol or not isinstance(asset, dict):
                raise ValueError("Invalid research asset")
            inception = _day(asset["inception"])
            if asset.get("currency") != self.currency:
                raise ValueError("A missing or different quote currency requires an explicit historical FX source")
            if not isinstance(asset.get("bars"), list):
                raise ValueError("Asset observations must be a list")
            rows, previous_day, previous_stamp = [], None, None
            for raw in asset["bars"]:
                day = _day(raw["date"])
                stamp = _number(raw.get("ts"), "source timestamp", minimum=1.0)
                utc_day = datetime.fromtimestamp(stamp, timezone.utc).date()
                if abs((utc_day - day).days) > 1:
                    raise ValueError("Source timestamp is inconsistent with its session date")
                if (previous_day is not None and day <= previous_day) or (previous_stamp is not None and stamp <= previous_stamp):
                    raise ValueError("Observations must have unique chronological dates and source timestamps")
                previous_day, previous_stamp = day, stamp
                _number(raw.get("close"), "raw close", minimum=1e-15)
                _number(raw.get("adjusted_close"), "adjusted close", minimum=1e-15)
                if day >= inception:
                    rows.append(dict(raw))
            self.bars[symbol] = rows
            self.dates[symbol] = [row["date"] for row in rows]
        self.calendar = sorted(set().union(*(set(days) for days in self.dates.values())))
        if not self.calendar:
            raise ValueError("No post-inception observations")
        self._prepared_cache: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}

    def history(self, symbol: str, cutoff: str):
        # Callers receive observations, not mutation authority over the panel.
        return [dict(row) for row in self.bars[symbol][:bisect_right(self.dates[symbol], cutoff)]]

    def observed(self, symbol: str, day: str):
        index = bisect_right(self.dates[symbol], day) - 1
        return self.bars[symbol][index] if index >= 0 and self.dates[symbol][index] == day else None

    def latest(self, symbol: str, day: str):
        index = bisect_right(self.dates[symbol], day) - 1
        return self.bars[symbol][index] if index >= 0 else None

    def mark(self, symbol: str, day: str):
        row = self.latest(symbol, day)
        if row is None:
            raise ValueError(f"Cannot value {symbol} at {day} without a preceding observation")
        # Daily NAV carries an exchange's last close through closures.  It must
        # not silently carry a missing crypto price or a long-stale fund quote.
        age = (_day(day) - _day(row["date"])).days
        limit = 2 if self.assets[symbol].get("category") == "crypto" else 7
        if age > limit:
            raise ValueError(f"Cannot value {symbol} at {day}: stale observed close")
        return row["close"]


def monthly_schedule(panel: PricePanel, config: Experiment) -> list[dict[str, Any]]:
    """Common source-session clock, independent of strategy and future prices.

    Active calendar membership uses catalog inception and observations available
    at the completed month-end.  A common observed session is a retrospective
    exchange-calendar proxy, explicitly not an archived exchange calendar.  A
    missing common session retains that month's contribution as cash on the
    first weekday, rather than silently dropping the external cash flow.
    """
    start, end = _day(config.start), _day(config.end)
    first = start.replace(day=1)
    result = []
    while first <= end:
        cutoff = first - timedelta(days=1)
        next_month = _month_start_after(first)
        active = sorted(symbol for symbol, asset in panel.assets.items()
                        if _day(asset["inception"]) <= cutoff and panel.latest(symbol, cutoff.isoformat()) is not None)
        sets = [set(day for day in panel.dates[symbol]
                    if first.isoformat() <= day < next_month.isoformat()) for symbol in active]
        common = set.intersection(*sets) if sets else set()
        if common:
            session = min(common)
            reason = None
        else:
            weekday = first
            while weekday.weekday() >= 5:
                weekday += timedelta(days=1)
            session = weekday.isoformat()
            reason = "no_common_observed_execution_session" if active else "no_incepted_observed_calendar_assets"
        if start.isoformat() <= session <= end.isoformat():
            result.append({"decision_session": cutoff.isoformat(), "execution_session": session,
                           "calendar_assets": active, "unavailable_reason": reason})
        first = next_month
    return result


def prepared_candidates(panel: PricePanel, cutoff: str, *, use_cache: bool = True):
    """Reuse only pure month-end preparation from one frozen source panel.

    Risk profile, costs, ownership, lot policy and challenger transformations
    are applied afterward.  Every caller gets its own deep copy, including
    metrics, raw scaffold and weekly return maps.  The runner freezes/hashes
    production source before comparisons; this cache never spans source runs.
    ``use_cache=False`` is an explicit parity/reference path for verification.
    """
    _day(cutoff)
    if not use_cache:
        return _prepare_candidates_uncached(panel, cutoff)
    if cutoff not in panel._prepared_cache:
        panel._prepared_cache[cutoff] = _prepare_candidates_uncached(panel, cutoff)
    return deepcopy(panel._prepared_cache[cutoff])


def _prepare_candidates_uncached(panel: PricePanel, cutoff: str):
    """Build the frozen medium-horizon scaffold with no old-account inputs."""
    model, scorer = load_stack(False)
    quality_module = _core_module("data_quality")
    cutoff_day, stamp = _day(cutoff), _end_stamp(cutoff)
    signal_start = _prior_year(cutoff_day).isoformat()
    candidates, excluded = [], []
    for symbol, asset in sorted(panel.assets.items()):
        history = panel.history(symbol, cutoff)
        if not history:
            excluded.append({"symbol": symbol, "excluded_reason": "not_yet_observed_after_inception"})
            continue
        signal = [row for row in history if row["date"] >= signal_start]
        risk_points = [{"ts": row["ts"], "value": row["adjusted_close"], "session_date": row["date"]}
                       for row in history]
        signal_points = [{"ts": row["ts"], "value": row["adjusted_close"], "session_date": row["date"]}
                         for row in signal]
        quality = {
            "quote": quality_module.assess_quote_freshness(history[-1]["ts"], analysis_time=stamp, category=asset["category"]),
            "signal": quality_module.assess_history_freshness(signal_points, analysis_time=stamp,
                                                               category=asset["category"], cadence="daily", role="signal"),
            "risk": quality_module.assess_history_freshness(risk_points, analysis_time=stamp,
                                                             category=asset["category"], cadence="daily", role="risk"),
        }
        blockers = sorted(set(reason for check in quality.values() for reason in check["reasons"]))
        if blockers:
            excluded.append({"symbol": symbol, "first": history[0]["date"], "last": history[-1]["date"],
                             "excluded_reason": ",".join(blockers), "source_quality": quality})
            continue
        scale = history[-1]["close"] / signal[-1]["adjusted_close"]
        prices = [row["adjusted_close"] * scale for row in signal]
        scaffold = scorer.analyze_prices(
            prices, current_price=history[-1]["close"], category=asset["category"],
            holding_weight=0.0, category_weight=0.0, portfolio_overlap=0.0,
            risk_tolerance=model.SIGNAL_SCAFFOLD_RISK, horizon="medium",
            strategy=model.VALIDATED_SIGNAL_STRATEGY, overlap_policy="allow",
        ).as_dict()
        metadata = {"provider": "yahoo" if panel.document.get("kind") == "observed_adjusted_history" else "synthetic",
                    "provider_id": symbol, "symbol": symbol, "name": asset["name"],
                    "category": asset["category"], "currency": asset["currency"]}
        weekly = model.weekly_return_map_from_points(risk_points, require_session_dates=True)
        candidate = model.prepare_scored_candidate(scaffold, metadata, weekly)
        if asset.get("sleeve") is not None and candidate["economic_sleeve"] != asset["sleeve"]:
            raise ValueError(f"Source catalog sleeve disagrees with production classification for {symbol}")
        candidate.update({
            "portfolio_price": history[-1]["close"], "price": history[-1]["close"],
            "first_observed_session": history[0]["date"], "last_observed_session": history[-1]["date"],
            "research_signal_first": signal[0]["date"], "research_signal_last": signal[-1]["date"],
            "research_signal_observations": len(signal), "research_scaffold_result": deepcopy(scaffold),
            "research_source_quality": quality,
        })
        candidates.append(candidate)
    scorer.attach_relative_strength(candidates)
    candidates.sort(key=lambda row: (float(row.get("score") or 0),
                                    float(row.get("relative_strength") if row.get("relative_strength") is not None else -1),
                                    float(row.get("confidence") or 0), str(row["symbol"])), reverse=True)
    return candidates, excluded


def _finalize(rows, config: Experiment, as_of: str):
    finalizer = _core_module("portfolio_plan").finalize_purchase_plan
    rows, summary = finalizer(
        rows, config.contribution, config.risk, existing_positions=(), existing_cash=0.0,
        execution_costs=config.costs().production_profile(), max_drawdown_loss=config.max_drawdown_loss,
        minimum_cash_reserve_fraction=config.minimum_cash_reserve_fraction,
        max_candidate_fraction=config.max_candidate_fraction, portfolio_context="ignore",
        as_of=as_of, bootstrap_repetitions=0,
    )
    scope = summary["portfolio_risk"]
    if (scope["scope"] != "new_contribution_only" or scope["existing_value"] != 0
            or scope["existing_cash"] != 0 or scope["purchase_budget"] != config.contribution):
        raise AssertionError("Old-account state entered contribution finalization")
    return rows, summary


def _cost_gate(rows, summary, config: Experiment, as_of: str, *, finalize=None):
    """Dropping one hedge can require another risk reduction and cost check."""
    diagnostics = []
    for _ in range(len(rows) + 1):
        filtered, detail = filter_approved_rows(config.strategy, rows, config.costs())
        diagnostics.append(detail)
        if not detail.get("removed"):
            return filtered, summary, diagnostics
        before = sum(float(row.get("suggested_units") or 0) > 0 for row in rows)
        after = sum(float(row.get("suggested_units") or 0) > 0 for row in filtered)
        if after >= before:
            raise AssertionError("Cost filter did not make a strictly downward removal")
        rows, summary = (finalize(filtered) if finalize is not None else _finalize(filtered, config, as_of))
    raise AssertionError("Downward cost/risk filtering failed to terminate")


def _candidate_summary(row):
    return {
        "symbol": row["symbol"], "first": row["first_observed_session"], "last": row["last_observed_session"],
        "signal_first": row["research_signal_first"], "signal_last": row["research_signal_last"],
        "signal_observations": row["research_signal_observations"],
        "weekly_count": row["risk_history_weeks"], "market_score": row.get("market_score"),
        "confidence": row.get("confidence"), "activation": row.get("activation"),
        "economic_sleeve": row.get("economic_sleeve"),
        "allocation_weight_exact_validated": row.get("allocation_weight_exact_validated", 0.0),
        "allocation_weight_after_constraints": row.get("allocation_weight_after_constraints", 0.0),
        "score_semantics": row.get("research_score_semantics", "frozen_composite_market_score"),
        "original_market_score": row.get("research_original_market_score", row.get("market_score")),
    }


def decide(panel: PricePanel, cutoff: str, config: Experiment, *, owned_symbols=()):
    """Return quote-approved orders; holdings can only remove owned identities."""
    model, _ = load_stack(False)
    candidates, exclusions = prepared_candidates(panel, cutoff)
    if config.existing_instruments == "exclude":
        owned = set(owned_symbols)
        kept = []
        for row in candidates:
            if row["symbol"] in owned:
                exclusions.append({**_candidate_summary(row), "excluded_reason": "already_owned_exact_source_symbol"})
            else:
                kept.append(row)
        candidates = kept
    candidates, transformations = transform_candidates(config.strategy, candidates, panel, cutoff, model)
    weighted, weight_meta = initial_weights(config.strategy, candidates, config.risk, model)
    candidates = weight_meta.get("candidates", candidates)
    constrained, constraints = model.apply_downward_weight_constraints(
        weighted, risk=config.risk, min_confidence=config.min_confidence,
        max_candidate_fraction=config.max_candidate_fraction,
        minimum_cash_reserve_fraction=config.minimum_cash_reserve_fraction,
    )
    exact_by_symbol = {row["symbol"]: weight for row, weight in weighted}
    constrained_by_symbol = {row["symbol"]: weight for row, weight in constrained}
    for row in candidates:
        row["allocation_weight_exact_validated"] = exact_by_symbol.get(row["symbol"], 0.0)
        row["allocation_weight_after_constraints"] = constrained_by_symbol.get(row["symbol"], 0.0)
        row["allocation_eligible"] = constrained_by_symbol.get(row["symbol"], 0.0) > 0.0
        row["whole_units_only"] = bool(config.whole_units_only or row["category"] in config.whole_unit_categories)
    rows, projection = model.production_projection(
        candidates, constrained, config.risk, config.contribution,
        max_candidate_fraction=config.max_candidate_fraction,
        max_candidate_fraction_is_hard=config.max_candidate_fraction_is_hard,
        whole_units_only=config.whole_units_only, whole_unit_categories=config.whole_unit_categories,
    )
    rows, summary = _finalize(rows, config, cutoff)
    recovery = _core_module("affordable_lots").recover_affordable_minimum_lots
    recovered = recovery({
        "mode": "deterministic", "results": rows, "evidence_as_of": _end_stamp(cutoff),
        "allocation": {**summary, "economic_sleeve_caps": dict(model.ECONOMIC_SLEEVE_CAPS[config.risk]),
                       "max_candidate_fraction": config.max_candidate_fraction},
        "preferences": {
            "whole_units_only": config.whole_units_only,
            "whole_unit_categories": list(config.whole_unit_categories),
            "min_cash_reserve_pct": config.minimum_cash_reserve_fraction * 100,
            "effective_max_candidate_pct": config.max_candidate_fraction * 100,
            "execution_costs": config.costs().production_profile(),
            "max_drawdown_pct": None if config.max_drawdown_loss is None else config.max_drawdown_loss * 100,
        },
    }, budget=config.contribution, risk_tolerance=config.risk)
    rows, summary = recovered["results"], recovered["allocation"]
    rows, summary, cost_filters = _cost_gate(rows, summary, config, cutoff)
    return rows, {
        "candidate_history": [_candidate_summary(row) for row in candidates] + exclusions,
        "quote_guard": summary,
        "policy": {"transformations": transformations,
                   "weights": {key: value for key, value in weight_meta.items() if key != "candidates"},
                   "constraints": constraints, "projection": projection,
                   "cost_filters": cost_filters},
    }


def _order_summary(row):
    return {"symbol": row["symbol"], "price": row["portfolio_price"],
            "suggested_amount": row.get("suggested_amount", 0.0),
            "suggested_units": row.get("suggested_units", 0.0),
            "whole_units_only": bool(row.get("whole_units_only"))}


def _stage_timestamp(session: str, stage: str) -> str:
    day = _day(session)
    if stage == "exchange_close":
        instant = datetime(day.year, day.month, day.day, 17, 30, tzinfo=ZoneInfo("Europe/Berlin"))
    elif stage == "crypto_close":
        instant = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=timezone.utc)
    else:
        raise ValueError("Unknown execution stage")
    return instant.astimezone(timezone.utc).isoformat()


def execute_orders(rows, panel: PricePanel, session: str, cutoff: str, config: Experiment):
    """Fill in causal category stages; earlier fills are immutable.

    The exchange stage can read only exchange closes.  Crypto daily closes are
    read later, at their own declared stage.  Earlier fills are holdings in this
    *same new-contribution bucket*, so later checks can reduce pending orders
    but cannot retrospectively cancel an exchange purchase.  The daily-close
    prices are hypothetical executable proxies, not broker fill evidence.
    """
    if _day(session) <= _day(cutoff):
        raise ValueError("Execution must follow the completed signal cutoff")
    approved = {row["symbol"]: deepcopy(row) for row in rows}
    if len(approved) != len(rows):
        raise ValueError("Duplicate approved instrument")
    balance = _cash(config.contribution, "contribution")
    reserve = (Decimal(str(config.contribution)) * Decimal(str(config.minimum_cash_reserve_fraction))).quantize(CENT, rounding=ROUND_CEILING)
    costs, trades, rejected, committed, stages, all_filters = config.costs(), [], [], [], [], []
    core = _core_module("portfolio_plan")
    for stage in ("exchange_close", "crypto_close"):
        stage_rows = [row for row in rows
                      if ("crypto_close" if row.get("category") == "crypto" else "exchange_close") == stage]
        if not stage_rows:
            continue
        instant = _stage_timestamp(session, stage)
        repriced = []
        for original in stage_rows:
            row = deepcopy(original)
            if float(row.get("suggested_units") or 0.0) > 0:
                bar = panel.observed(row["symbol"], session)
                if bar is None:
                    row.update(suggested_amount=0.0, suggested_units=0.0, allocation_eligible=False)
                    rejected.append({"symbol": row["symbol"], "reason": "no_execution_observation", "stage": stage})
                else:
                    row.update(portfolio_price=bar["close"], price=bar["close"])
            repriced.append(row)
        cash_before = balance
        committed_before = {row["symbol"]: row["quantity"] for row in committed}

        def finalize_pending(pending):
            # Shared core's existing-position input here contains ONLY fills
            # already committed within this one contribution.  The outer
            # account's old positions/cash are not accepted by this function.
            if not committed:
                return _finalize(pending, config, session)
            reserve_fraction = float(reserve / cash_before) if cash_before > ZERO else 0.0
            result, summary = core.finalize_purchase_plan(
                pending, float(cash_before), config.risk,
                existing_positions=committed, existing_cash=0.0,
                execution_costs=costs.production_profile(), max_drawdown_loss=config.max_drawdown_loss,
                minimum_cash_reserve_fraction=reserve_fraction,
                max_candidate_fraction=config.max_candidate_fraction, portfolio_context="use",
                as_of=session, bootstrap_repetitions=0,
            )
            summary["research_scope"] = "already_filled_positions_within_this_new_contribution_only"
            summary["original_new_contribution"] = config.contribution
            return result, summary

        final_rows, stage_guard = finalize_pending(repriced)
        final_rows, stage_guard, filters = _cost_gate(
            final_rows, stage_guard, config, session, finalize=finalize_pending,
        )
        all_filters.extend(dict(detail, stage=stage) for detail in filters)
        stage_trades = []
        for row in final_rows:
            original = approved[row["symbol"]]
            units = Decimal(str(row.get("suggested_units") or 0.0))
            limit_units = Decimal(str(original.get("suggested_units") or 0.0))
            limit_amount = Decimal(str(original.get("suggested_amount") or 0.0))
            if units < 0 or units > limit_units:
                raise AssertionError("A later execution price increased approved units")
            if units == 0:
                if limit_units > 0 and not any(item["symbol"] == row["symbol"] for item in rejected):
                    rejected.append({"symbol": row["symbol"], "reason": "reduced_to_zero_at_fill_validation", "stage": stage})
                continue
            if row.get("whole_units_only") and units != units.to_integral_value():
                raise AssertionError("A whole-unit order became fractional")
            bar = panel.observed(row["symbol"], session)
            if bar is None:
                raise AssertionError("Missing execution observation escaped the gate")
            price = Decimal(str(bar["close"]))
            principal = price * units
            if principal > limit_amount:
                raise AssertionError("Executed principal exceeded quote-approved authority")
            fee, rounding, debit = costs.exact_charge(principal)
            if debit != Decimal(str(row["estimated_cash_debit"])):
                raise AssertionError("Independent cash debit disagrees with the final cost engine")
            balance -= debit
            trade = {"symbol": row["symbol"], "session": session, "stage": stage,
                     "execution_timestamp": instant, "units": float(units),
                     "price": float(price), "principal": float(principal), "fees": float(fee),
                     "rounding_cost": float(rounding), "cash_debit": float(debit),
                     "approved_amount": float(limit_amount), "approved_units": float(limit_units),
                     "whole_units_only": bool(row.get("whole_units_only"))}
            trades.append(trade)
            stage_trades.append(trade)
            committed.append(dict(row, quantity=float(units)))
        if balance < reserve or balance < ZERO:
            raise AssertionError("Execution spent unavailable new-contribution cash or its reserve")
        if abs(float(balance) - float(stage_guard["execution_costs"]["cash_remaining"])) > 1e-9:
            raise AssertionError("Independent remaining cash disagrees with the stage finalizer")
        stages.append({
            "stage": stage, "execution_timestamp": instant,
            "cash_before": float(cash_before), "cash_after": float(balance),
            "committed_units_before": committed_before,
            "orders": [_order_summary(row) for row in repriced],
            "guard": stage_guard, "trades": stage_trades, "cost_filters": filters,
        })

    # Read-only verification of the complete bucket.  Executed quantities are
    # immutable; a failed post-check raises rather than undoing earlier fills.
    _, empty_guard = _finalize([], config, session)
    values = [(row, float(Decimal(str(row["quantity"])) * Decimal(str(row["portfolio_price"]))))
              for row in committed]
    post, _, issues = core._snapshot(values, float(balance), as_of=session,
                                    require_drawdown=config.max_drawdown_loss is not None)
    limits = empty_guard["portfolio_risk"]["limits"]
    breaches = core._breaches(post, limits)
    if issues or breaches:
        raise AssertionError(f"Committed contribution bucket failed its read-only final check: {issues + breaches}")
    total_debit = Decimal(str(config.contribution)) - balance
    principal = sum((Decimal(str(row["quantity"])) * Decimal(str(row["portfolio_price"])) for row in committed), ZERO)
    guard = {
        "stages": stages,
        "execution_costs": {
            "status": "confirmed", "profile": costs.production_profile(),
            "budget": config.contribution, "principal": float(principal),
            "estimated_cash_debit": float(total_debit),
            "estimated_transaction_cost": float(total_debit - principal),
            "cash_remaining": float(balance), "reserve_amount": float(reserve),
        },
        "portfolio_risk": {
            "scope": "new_contribution_only", "status": "within_limits",
            "existing_value": 0.0, "existing_cash": 0.0,
            "purchase_budget": config.contribution, "contribution": config.contribution,
            "already_filled_in_this_contribution": {row["symbol"]: row["quantity"] for row in committed},
            "post": post, "limits": limits, "post_breaches": breaches, "blockers": issues,
            "read_only_after_execution": True,
        },
    }
    return balance, trades, rejected, guard, all_filters


def _positions_value(panel: PricePanel, quantities: dict[str, Decimal], session: str) -> Decimal:
    return sum((quantity * Decimal(str(panel.mark(symbol, session)))
                for symbol, quantity in quantities.items() if quantity > ZERO), ZERO)


def unitized_metrics(points, *, beginning_nav=1.0, beginning_date=None):
    if not points:
        return {"observations": 0}
    navs = [beginning_nav] + [point["nav"] for point in points]
    returns = [later / earlier - 1.0 for earlier, later in zip(navs, navs[1:]) if earlier > 0]
    peak, drawdown = beginning_nav, 0.0
    underwater = maximum_underwater = 0
    for value in navs[1:]:
        if value >= peak - 1e-12:
            peak, underwater = max(peak, value), 0
        else:
            underwater += 1
            maximum_underwater = max(maximum_underwater, underwater)
        drawdown = max(drawdown, 1.0 - value / peak)
    # Sunday closes define complete observed calendar weeks.  The first return
    # needs two successive Sundays; an incomplete final week is never treated
    # as a full weekly loss or volatility observation.
    sundays = [point for point in points if _day(point["date"]).weekday() == 6]
    weekly = [later["nav"] / earlier["nav"] - 1.0
              for earlier, later in zip(sundays, sundays[1:])
              if (_day(later["date"]) - _day(earlier["date"])).days == 7 and earlier["nav"] > 0]
    first = _day(beginning_date) if beginning_date else _day(points[0]["date"]) - timedelta(days=1)
    years = max(1 / 365.25, (_day(points[-1]["date"]) - first).days / 365.25)
    ratio = points[-1]["nav"] / beginning_nav
    return {
        "observations": len(points), "first_session": points[0]["date"], "last_session": points[-1]["date"],
        "time_weighted_total_return_after_costs": ratio - 1.0,
        "time_weighted_cagr_after_costs": ratio ** (1.0 / years) - 1.0 if ratio > 0 else -1.0,
        "annualized_weekly_volatility": statistics.pstdev(weekly) * math.sqrt(52.0) if len(weekly) > 1 else None,
        "annualized_calendar_daily_volatility": statistics.pstdev(returns) * math.sqrt(365.25) if len(returns) > 1 else None,
        "max_unitized_drawdown": drawdown, "longest_underwater_calendar_days": maximum_underwater,
        "empirical_weekly_es95_after_costs": exact_es(weekly), "weekly_observations": len(weekly),
        "ending_nominal_wealth": points[-1]["wealth"], "ending_cash": points[-1]["cash"],
        "ending_cash_fraction": points[-1]["cash"] / points[-1]["wealth"] if points[-1]["wealth"] else 1.0,
    }


def replay(document, config: Experiment, *, capture_trace=True):
    """Return deterministic decisions, cash-flow-adjusted results and raw ledger."""
    if not isinstance(config, Experiment):
        raise TypeError("An immutable Experiment is required")
    panel = document if isinstance(document, PricePanel) else PricePanel(document)
    if config.end > panel.calendar[-1]:
        raise ValueError("Replay end exceeds the source snapshot's final observation")
    quantities = {symbol: Decimal(str(quantity)) for symbol, quantity in config.initial_positions}
    if any(symbol not in panel.assets for symbol in quantities):
        raise ValueError("Initial position is outside the frozen source universe")
    start, end = _day(config.start), _day(config.end)
    before_start = (start - timedelta(days=1)).isoformat()
    cash = _cash(config.initial_cash, "initial_cash")
    initial_capital = cash + _positions_value(panel, quantities, before_start)
    issued_units, nav = initial_capital, Decimal(1)
    schedule = {item["execution_session"]: item for item in monthly_schedule(panel, config)}
    trace, points, trades_all = [], [], []
    contribution_total = ZERO
    day = start
    with localcontext() as context:
        context.prec = 60
        while day <= end:
            session = day.isoformat()
            contribution = ZERO
            if session in schedule:
                item = schedule[session]
                cutoff = item["decision_session"]
                contribution = _cash(config.contribution, "contribution")
                old_cash = cash
                previous_day = (day - timedelta(days=1)).isoformat()
                # External money arrives at 00:00 UTC.  Only the previous
                # completed day's marks can price this issue of fund units.
                before_flow = cash + _positions_value(panel, quantities, previous_day)
                if issued_units > ZERO:
                    nav = before_flow / issued_units
                if nav <= ZERO:
                    raise ValueError("Cannot unitize a new contribution after total account loss")
                pre_flow_nav, units_before_flow = nav, issued_units
                issued_units += contribution / nav
                contribution_total += contribution
                owned = [symbol for symbol, quantity in quantities.items() if quantity > ZERO]
                orders, detail = decide(panel, cutoff, config, owned_symbols=owned)
                if item["unavailable_reason"]:
                    orders = [dict(row, suggested_amount=0.0, suggested_units=0.0, allocation_eligible=False) for row in orders]
                    orders, detail["quote_guard"] = _finalize(orders, config, cutoff)
                unspent, trades, rejected, fill_guard, fill_filters = execute_orders(orders, panel, session, cutoff, config)
                if item["unavailable_reason"]:
                    rejected.append({"symbol": None, "reason": item["unavailable_reason"]})
                cash = old_cash + unspent
                for trade in trades:
                    symbol = trade["symbol"]
                    quantities[symbol] = quantities.get(symbol, ZERO) + Decimal(str(trade["units"]))
                trades_all.extend(trades)
                trace.append({
                    "decision_session": cutoff, "execution_session": session,
                    "contribution_timestamp": session + "T00:00:00+00:00",
                    "pre_flow_nav": float(pre_flow_nav), "pre_flow_wealth": float(before_flow),
                    "issued_units_before_flow": float(units_before_flow),
                    "calendar_assets": item["calendar_assets"], "contribution": float(contribution),
                    "quote_budget": config.contribution, "prior_unspent_cash_not_reoffered": float(old_cash),
                    "cash_after_execution": float(cash), "new_contribution_cash_remaining": float(unspent),
                    "candidate_history": detail["candidate_history"],
                    "orders": [_order_summary(row) for row in orders], "trades": trades,
                    "execution_rejections": rejected, "quote_guard": detail["quote_guard"],
                    "fill_guard": fill_guard, "policy": detail["policy"], "fill_cost_filters": fill_filters,
                })
            positions_value = _positions_value(panel, quantities, session)
            wealth = cash + positions_value
            nav = wealth / issued_units if issued_units > ZERO else Decimal(1)
            points.append({"date": session, "wealth": float(wealth), "cash": float(cash),
                           "positions_value": float(positions_value), "nav": float(nav),
                           "issued_units": float(issued_units), "contribution": float(contribution)})
            day += timedelta(days=1)
    metrics = unitized_metrics(points, beginning_date=before_start)
    fees = math.fsum(trade["fees"] + trade["rounding_cost"] for trade in trades_all)
    supplied = float(initial_capital + contribution_total)
    guard_failures = sum(entry["fill_guard"]["portfolio_risk"]["status"] != "within_limits" for entry in trace)
    metrics.update({
        "initial_capital": float(initial_capital), "contributions": float(contribution_total),
        "net_gain_after_costs": metrics["ending_nominal_wealth"] - supplied,
        "total_execution_costs": fees, "costs_fraction_of_capital_supplied": fees / supplied if supplied else 0.0,
        "trade_count": len(trades_all), "decision_count": len(trace),
        "abstaining_decisions": sum(not bool(entry["trades"]) for entry in trace),
        "execution_rejections": sum(len(entry["execution_rejections"]) for entry in trace),
        "fill_guard_nonpassing_decisions": guard_failures,
        "deployed_marked_principal": math.fsum(trade["principal"] for trade in trades_all),
        "old_cash_reoffered": False,
    })
    development = [point for point in points if point["date"] <= config.development_end]
    evaluation = [point for point in points if point["date"] > config.development_end]
    evaluation_nav = development[-1]["nav"] if development else 1.0
    evaluation_date = development[-1]["date"] if development else before_start
    return {
        "schema": SCHEMA, "parameters": asdict(config), "metrics": metrics,
        "development": unitized_metrics(development, beginning_date=before_start),
        "evaluation": unitized_metrics(evaluation, beginning_nav=evaluation_nav, beginning_date=evaluation_date),
        "trace": trace if capture_trace else [], "daily_accounting": points if capture_trace else [],
        "ending_positions": {symbol: float(quantity) for symbol, quantity in sorted(quantities.items())},
        "trades": trades_all if capture_trace else [],
        "execution_contract": {
            "decision_scope": "new_contribution_only", "existing_cash_reoffered": False,
            "holdings_use": "exact_source_symbol_filter_only" if config.existing_instruments == "exclude" else "outcome_accounting_only",
            "signal_cutoff": "previous_completed_calendar_month_end",
            "signal_window": "one_calendar_year_inclusive_with_february_leap_day_clamp",
            "execution_calendar": "first_common_observed_session_of_month_across_incepted_source_universe",
            "calendar_is_historical_observation_proxy": True,
            "quantity_authority": "literal_post_quote_gate_quantity_and_principal_ceilings",
            "price_gap_policy": "repricing_may_only_reduce_approved_units_and_principal",
            "execution_stages": ["exchange_close_17_30_Europe_Berlin", "crypto_close_23_59_59_UTC"],
            "later_stage_may_change_earlier_fills": False,
            "execution_prices": "hypothetical_daily_close_proxies_not_broker_fills",
            "risk_at_fill": "causal_stage_gate_with_prior_fills_in_the_same_contribution_immutable",
            "risk_history_at_fill": "only_observations_through_previous_calendar_month_end",
            "contribution_event": "00_00_UTC_on_execution_date_at_previous_completed_day_NAV",
            "initial_valuation_date": before_start,
            "performance_calendar": "every_calendar_day_with_last_observed_exchange_close",
            "performance_volatility": "successive_complete_sunday_nav_returns_times_sqrt52",
            "cash_return": "zero_nominal_interest", "sales": False, "leverage": False,
            "fees_are_hypothetical_declared_scenarios": True,
            "historical_provider_and_identifier_state_reconstructed": False,
            "source_vintage": "current_adjusted_history_not_point_in_time_archive",
            "prospective_evidence": False,
        },
    }
