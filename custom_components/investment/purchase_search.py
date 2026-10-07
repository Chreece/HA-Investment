"""Bounded, deterministic enumeration of downward whole-unit purchase choices.

The portfolio finalizer owns eligibility and risk approval. This helper only
enumerates integer quantities under the original positive purchase ceilings and
prices them with the same confirmed Decimal cash convention. Fractional rows
remain fixed at the already verified proportional incumbent. An exhaustive
integer domain is not an optimum over fractional allocations, prior model
weights, or future investment outcomes.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from fractions import Fraction
from itertools import product
from typing import Any

from .execution_costs import validate_execution_costs
from .validated_model import _candidate_evidence_eligible


MAX_WHOLE_SEARCH_STATES = 1024
MAX_SEARCH_RETURN_POINTS = 1_000_000
_MAX_BEAM_WIDTH = 128
_MAX_REPORTED_DOMAIN = 10**18
_CENT = Decimal("0.01")
_ZERO = Decimal(0)


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    result = Decimal(str(value))
    return result if result.is_finite() else None


def _identity(row: dict[str, Any], index: int) -> tuple[str, str, str, int]:
    """Provider identities break ties; input order only resolves exact duplicates."""
    return (
        str(row.get("provider") or ""),
        str(row.get("provider_id") or row.get("symbol") or ""),
        str(row.get("symbol") or ""), index,
    )


@dataclass(frozen=True)
class WholeCandidate:
    index: int
    identity: tuple[str, str, str, int]
    price: Decimal
    maximum: int
    incumbent: int


@dataclass(frozen=True)
class SearchState:
    counts: tuple[int, ...]
    principal: Decimal
    debit: Decimal
    deviation: Fraction

    @property
    def score(self) -> tuple[Any, ...]:
        return (self.principal, -(self.debit - self.principal), -self.deviation, self.counts)


@dataclass
class WholeSearch:
    candidates: list[WholeCandidate]
    states: list[SearchState]
    incumbent: SearchState
    available: Decimal
    metadata: dict[str, Any]


def search_metadata(principal: float = 0.0) -> dict[str, Any]:
    return {
        "contract": "bounded-whole-unit-reductions-v1",
        "status": "not_applicable",
        "domain_scope": "original_positive_whole_units_with_fixed_incumbent_fractional_quantities",
        "fractional_incumbent_fixed": True,
        "objective": ["maximum_marked_principal", "minimum_estimated_transaction_cost",
                      "minimum_relative_whole_target_deviation", "stable_instrument_identity"],
        "total_combinations": 0, "state_limit": max(1, int(MAX_WHOLE_SEARCH_STATES)),
        "generated": 0, "examined": 0, "affordable": 0, "feasible": 0,
        "objective_pruned": 0, "cash_pruned": 0,
        "risk_rejected": 0, "risk_rejection_issues": [],
        "optimality_proven": False, "global_optimality_claimed": False,
        "no_positive_feasible": None,
        "selected_source": "proportional_incumbent",
        "initial_principal": principal, "selected_principal": principal,
        "improvement": False, "whole_candidates": 0, "fractional_candidates": 0,
        "full_domain_enumerated": False,
        "risk_evaluation_limit": 0, "stopped_by_work_limit": False,
    }


def risk_evaluation_limit(rows: list[dict[str, Any]], positions: list[dict[str, Any]]) -> int:
    """Bound repeated risk work as well as the number of enumerated states."""
    history_points = sum(
        max(1, len(history)) if isinstance(history, (dict, list, tuple)) else 1
        for row in [*rows, *positions]
        for history in (row.get("risk_weekly_returns"),)
    )
    return max(1, min(max(1, int(MAX_WHOLE_SEARCH_STATES)),
                      max(1, int(MAX_SEARCH_RETURN_POINTS)) // max(1, history_points)))


def prepare_whole_search(
    original: list[dict[str, Any]], incumbent_rows: list[dict[str, Any]],
    budget: float, reserve_amount: float, profile: dict[str, Any],
    *, amount_key: str, units_key: str,
) -> WholeSearch:
    with localcontext() as context:
        context.prec = 60
        return _prepare_whole_search(
            original, incumbent_rows, budget, reserve_amount, profile,
            amount_key=amount_key, units_key=units_key,
        )


def _prepare_whole_search(
    original: list[dict[str, Any]], incumbent_rows: list[dict[str, Any]],
    budget: float, reserve_amount: float, profile: dict[str, Any],
    *, amount_key: str, units_key: str,
) -> WholeSearch:
    checked = validate_execution_costs(profile)
    if checked["status"] != "confirmed":
        raise ValueError("Whole-unit search requires confirmed execution costs")
    fixed = Decimal(str(checked["fixed_fee"]))
    rate = (Decimal(str(checked["commission_pct"])) / 100
            + Decimal(str(checked["spread_bps"])) / 10000
            + Decimal(str(checked["fx_bps"])) / 10000)
    available = (Decimal(str(budget)).quantize(_CENT, rounding=ROUND_FLOOR)
                 - Decimal(str(reserve_amount)).quantize(_CENT, rounding=ROUND_CEILING))
    available = max(_ZERO, available)

    def debit(price: Decimal, count: int | Decimal) -> Decimal:
        principal = price * count
        return (principal * (1 + rate) + fixed).quantize(
            _CENT, rounding=ROUND_CEILING) if principal > 0 else _ZERO

    candidates: list[WholeCandidate] = []
    for index, row in enumerate(original):
        if row.get("whole_units_only") is not True or row.get("allocation_eligible") is False:
            continue
        if not _candidate_evidence_eligible(row):
            continue
        amount, units = _decimal(row.get(amount_key)), _decimal(row.get(units_key))
        price = _decimal(row.get("portfolio_price") if "portfolio_price" in row else row.get("price"))
        if (amount is None or not _ZERO < amount <= Decimal("1e15")
                or units is None or not _ZERO < units <= Decimal("1e24")
                or price is None or not _ZERO < price <= Decimal("1e15")):
            continue
        maximum = int(min(units, amount / price).to_integral_value(rounding=ROUND_FLOOR))
        if maximum <= 0:
            continue
        incumbent = _decimal(incumbent_rows[index].get(units_key)) or _ZERO
        candidates.append(WholeCandidate(index, _identity(row, index), price, maximum,
                                         min(maximum, max(0, int(incumbent)))))
    candidates.sort(key=lambda candidate: candidate.identity)
    indices = {candidate.index for candidate in candidates}
    base_principal, base_debit = _ZERO, _ZERO
    fractional_candidates = 0
    for index, row in enumerate(incumbent_rows):
        if index in indices:
            continue
        units = _decimal(row.get(units_key)) or _ZERO
        if units <= 0:
            continue
        price = _decimal(row.get("portfolio_price") if "portfolio_price" in row else row.get("price"))
        if price is None or price <= 0:
            raise ValueError("Verified incumbent has an unavailable purchase price")
        base_principal += units * price
        base_debit += debit(price, units)
        if row.get("whole_units_only") is not True:
            fractional_candidates += 1

    def state(counts: tuple[int, ...]) -> SearchState:
        principal, cash_debit, deviation = base_principal, base_debit, Fraction(0)
        for candidate, count in zip(candidates, counts, strict=True):
            principal += candidate.price * count
            cash_debit += debit(candidate.price, count)
            deviation += Fraction(candidate.maximum - count, candidate.maximum)
        return SearchState(counts, principal, cash_debit, deviation)

    incumbent_counts = tuple(candidate.incumbent for candidate in candidates)
    incumbent = state(incumbent_counts)
    metadata = search_metadata(float(incumbent.principal))
    metadata.update(whole_candidates=len(candidates), fractional_candidates=fractional_candidates)
    if not candidates:
        return WholeSearch(candidates, [], incumbent, available, metadata)
    total_combinations = 1
    exact_total = True
    for candidate in candidates:
        if total_combinations > _MAX_REPORTED_DOMAIN // (candidate.maximum + 1):
            exact_total = False
            break
        total_combinations *= candidate.maximum + 1
    metadata["total_combinations"] = total_combinations if exact_total else None
    maxima = tuple(candidate.maximum for candidate in candidates)
    if incumbent_counts == maxima:
        metadata.update(status="upper_bound_verified", optimality_proven=True,
                        no_positive_feasible=False)
        return WholeSearch(candidates, [], incumbent, available, metadata)

    limit = metadata["state_limit"]
    if exact_total and total_combinations <= limit:
        states = [state(counts) for counts in product(*(range(candidate.maximum + 1) for candidate in candidates))]
        metadata.update(status="exhaustive", full_domain_enumerated=True)
    else:
        metadata["status"] = "bounded"
        # A narrow beam keeps both high deployment and representatives across
        # deployment bands. Every retained result still needs the portfolio gate.
        # Cheap cash pruning cannot discard an affordable positive purchase.
        beam_width = min(_MAX_BEAM_WIDTH, limit)
        beam = [SearchState((), base_principal, base_debit, Fraction(0))]
        for candidate in candidates:
            remaining_for_one = available - base_debit - fixed
            affordable_max = max(0, min(candidate.maximum, int(
                (remaining_for_one / (candidate.price * (1 + rate))).to_integral_value(rounding=ROUND_FLOOR)
            ))) if remaining_for_one > 0 else 0
            if affordable_max <= 12:
                options = set(range(affordable_max + 1))
            else:
                options = {0, 1, affordable_max, affordable_max - 1}
                options.update(affordable_max * part // 8 for part in range(1, 8))
            options.update(value for value in (candidate.incumbent - 1, candidate.incumbent,
                                                candidate.incumbent + 1) if 0 <= value <= affordable_max)
            increments = [(count, candidate.price * count, debit(candidate.price, count),
                           Fraction(candidate.maximum - count, candidate.maximum)) for count in sorted(options)]
            expanded = [SearchState(prior.counts + (count,), prior.principal + principal,
                                    prior.debit + cash_debit, prior.deviation + deviation)
                        for prior in beam for count, principal, cash_debit, deviation in increments
                        if prior.debit + cash_debit <= available]
            expanded.sort(key=lambda item: item.score, reverse=True)
            selected = {item.counts: item for item in expanded[:max(1, beam_width // 2)]}
            bands: set[int] = set()
            for item in expanded:
                band = int(min(64, (item.principal - base_principal) * 64 / max(_CENT, available)))
                if band in bands:
                    continue
                bands.add(band)
                selected.setdefault(item.counts, item)
                if len(selected) >= beam_width:
                    break
            for item in expanded:
                if len(selected) >= beam_width:
                    break
                selected.setdefault(item.counts, item)
            zero_prefix = tuple(0 for _ in range(len(beam[0].counts) + 1))
            if zero_prefix not in selected:
                zero_state = next((item for item in expanded if item.counts == zero_prefix), None)
                if zero_state is not None:
                    if len(selected) >= beam_width:
                        selected.pop(next(reversed(selected)))
                    selected[zero_prefix] = zero_state
            beam = list(selected.values())
            if not beam:
                break
        seeds: set[tuple[int, ...]] = {tuple(0 for _ in candidates), maxima, incumbent_counts}
        seeds.update(tuple(candidate.maximum * part // 32 for candidate in candidates) for part in range(1, 32))
        for index, candidate in enumerate(candidates):
            for count in (1, candidate.incumbent, candidate.maximum):
                counts = [0] * len(candidates)
                counts[index] = count
                seeds.add(tuple(counts))
            for base in (maxima, incumbent_counts):
                for count in (0, max(0, base[index] - 1)):
                    counts = list(base)
                    counts[index] = count
                    seeds.add(tuple(counts))
        seeds.update(item.counts for item in beam if len(item.counts) == len(candidates))
        all_states = sorted((state(counts) for counts in seeds), key=lambda item: item.score, reverse=True)
        # Keep the incumbent and zero even at tiny test/runtime bounds. Prioritize
        # affordable states because an unaffordable vector cannot beat cash.
        affordable_states = [item for item in all_states if item.debit <= available]
        selected_states = {item.counts: item for item in affordable_states[:limit]}
        for counts in (incumbent_counts, tuple(0 for _ in candidates)):
            if counts not in selected_states:
                if len(selected_states) >= limit:
                    selected_states.pop(next(reversed(selected_states)))
                selected_states[counts] = state(counts)
        states = list(selected_states.values())
    states.sort(key=lambda item: item.score, reverse=True)
    metadata.update(generated=len(states), affordable=sum(item.debit <= available for item in states),
                    cash_pruned=sum(item.debit > available for item in states))
    return WholeSearch(candidates, states, incumbent, available, metadata)


def proposed_rows(
    search: WholeSearch, state: SearchState, original: list[dict[str, Any]],
    incumbent: list[dict[str, Any]], *, amount_key: str, units_key: str,
) -> list[dict[str, Any]]:
    """Keep original authority while asking the cost engine for exact integers."""
    rows = [dict(row) for row in incumbent]
    for candidate, count in zip(search.candidates, state.counts, strict=True):
        # Use the incoming amount ceiling, not a freshly rounded price*quantity.
        # The exact integer unit request supplies the downward purchase bound.
        row = dict(original[candidate.index])
        row[amount_key] = original[candidate.index][amount_key] if count > 0 else 0.0
        row[units_key] = count
        rows[candidate.index] = row
    return rows
