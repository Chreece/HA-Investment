"""Independent, generated whole-unit feasibility audit; no market-return fitting.

The oracle enumerates small integer domains and independently rebuilds cash,
costs, exposures, volatility, empirical ES and historical drawdown. It does not
call a production feasibility, scoring, pricing or risk helper. The economic
policy values below are frozen inputs, not an alternative policy.

This is a deterministic engineering audit, not an investment performance study.
It cannot establish prospective returns or optimality outside the declared
downward integer domain. Every attempted case is retained before continuing.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from functools import lru_cache
import hashlib
import importlib
import itertools
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "investment"
BASELINE_COMMIT = "c9ca23ffc4a435b4058406ee57183938c0bb9af9"
BASELINE_FILES = (
    "portfolio_plan.py", "execution_costs.py", "risk_evidence.py",
    "validated_model.py", "instrument_identity.py",
)
LIVE_FILES = (*BASELINE_FILES, "purchase_search.py")
AS_OF = "2026-10-06"
SEED = 20261007
GENERATED_CASES = 48
CENT = Decimal("0.01")
ZERO = Decimal(0)
TOLERANCE = 1e-12
SLEEVES = (
    "cash_like", "government_bond", "aggregate_bond", "broad_equity",
    "sector_equity", "single_equity", "commodity", "crypto",
)
POLICY = {
    "low": {"volatility": .10 * 1.01, "es": 2.0627128075074257 * .10 / math.sqrt(52) * 1.01,
            "sleeves": dict(zip(SLEEVES, (.50, .45, .40, .35, .10, .15, .10, .03), strict=True))},
    "medium": {"volatility": .16 * 1.01, "es": 2.0627128075074257 * .16 / math.sqrt(52) * 1.01,
               "sleeves": dict(zip(SLEEVES, (.45, .45, .45, .55, .25, .30, .15, .10), strict=True))},
}
ZERO_COSTS = {"confirmed": True, "fixed_fee": 0., "commission_pct": 0., "spread_bps": 0., "fx_bps": 0.}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def decimal_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    number = Decimal(str(value))
    return number if number.is_finite() else None


def make_row(name, price, units=1, sleeve="government_bond", *, values=None, **changes):
    if values is None:
        values = [.002, -.002] * 26
    history = {(date(2024, 1, 1) + timedelta(weeks=index)).strftime("%G-W%V"): value
               for index, value in enumerate(values)}
    return {"provider": "audit", "provider_id": name, "symbol": name, "category": "stock",
            "economic_sleeve": sleeve, "portfolio_price": price, "quantity": 0.,
            "suggested_amount": float(Decimal(str(price)) * Decimal(str(units))),
            "suggested_units": units, "whole_units_only": True,
            "allocation_eligible": True, "risk_weekly_returns": history, **changes}


def case(name, rows, *, budget=100., holdings=(), cash=0., risk="low", costs=None,
         reserve=0., cap=.45, drawdown=None):
    return {"name": name, "rows": rows, "budget": budget, "holdings": list(holdings),
            "cash": cash, "risk": risk, "costs": costs if costs is not None else {**ZERO_COSTS, "fixed_fee": 1.},
            "reserve": reserve, "cap": cap, "drawdown": drawdown}


def predefined_cases():
    basic = [make_row("A", 40., sleeve="cash_like"), make_row("B", 30.),
             make_row("C", 30., sleeve="aggregate_bond")]
    yield case("fixed_fee_all_or_nothing_counterexample", deepcopy(basic))
    yield case("same_sleeve_limit", [make_row(letter, 30.) for letter in "ABC"])
    yield case("no_affordable_positive_lot", [make_row("A", 99.)], costs={**ZERO_COSTS, "fixed_fee": 2.})
    yield case("cash_reserve_is_not_spendable", deepcopy(basic), reserve=.30)
    yield case("raw_price_and_principal_ceiling", [make_row("A", 10.004, suggested_amount=10.),
               make_row("B", 30.003, 2)], budget=61.019, cash=200., costs={**ZERO_COSTS, "fixed_fee": .001})
    held = make_row("HELD", 10., sleeve="broad_equity", quantity=80.)
    yield case("preexisting_breach_fixed_fee_strict_abstention", [make_row("B", 10., 2)],
               holdings=[held], cash=200., risk="medium", cap=None)
    yield case("preexisting_breach_zero_fee_nonworsening", [make_row("B", 10., 2)],
               holdings=[held], cash=200., risk="medium", cap=None, costs=ZERO_COSTS)
    volatile = [.08, -.08] * 26
    yield case("hedge_removal_requires_full_portfolio_check",
               [make_row("SAME", 40., sleeve="broad_equity", values=volatile),
                make_row("HEDGE", 40., values=[-value for value in volatile])],
               holdings=[make_row("HELD", 10., sleeve="broad_equity", quantity=10., values=volatile)], cash=100.)
    unknown_candidate = deepcopy(basic)
    unknown_candidate[0]["risk_weekly_returns"] = {}
    yield case("candidate_missing_history_cannot_enter_subset", unknown_candidate)
    unknown_held = make_row("UNKNOWN", 10., quantity=1., risk_weekly_returns={})
    yield case("unknown_held_history_still_blocks", deepcopy(basic), holdings=[unknown_held])
    failed_identity = deepcopy(basic)
    failed_identity[0]["instrument_identity_eligible"] = False
    yield case("failed_identity_cannot_be_restored", failed_identity)
    duplicate = deepcopy(basic)
    duplicate[1].update(provider_id="A", economic_sleeve="cash_like")
    yield case("duplicate_provider_exposure_aggregates", duplicate)
    yield case("drawdown_bound_checks_entire_selected_subset", [
        make_row("A", 30., sleeve="cash_like", values=[-.01] * 52),
        make_row("B", 30., sleeve="broad_equity", values=[-.02] * 52),
        make_row("C", 30.)], drawdown=.20)
    yield case("noninteger_unit_ceiling_is_floored", [make_row("A", 40., 1.9, sleeve="cash_like"),
               make_row("B", 30.)], cash=100.)


def generated_cases(count=GENERATED_CASES):
    """Fixed seed and bounded domains; no outcome-dependent rejection sampling."""
    rng = random.Random(SEED)
    for index in range(count):
        rows = []
        for j in range(rng.randrange(2, 5)):
            price = float(Decimal(rng.randrange(10, 601)) / 10 + rng.choice([ZERO, Decimal(".004"), Decimal(".009")]))
            units = rng.randrange(1, 4)
            amplitude = rng.choice([.002, .02, .08])
            drift = rng.choice([0., 0., -.003])
            sign = rng.choice([-1, 1])
            values = [drift + sign * amplitude * (1 if week % 2 else -1) for week in range(52)]
            row = make_row(f"R{j}", price, units, rng.choice(SLEEVES[:7]), values=values)
            if rng.randrange(8) == 0:
                row["suggested_amount"] = float(Decimal(str(row["suggested_amount"])) - Decimal(".001"))
            if rng.randrange(15) == 0:
                row["allocation_eligible"] = False
            rows.append(row)
        holdings = []
        if index % 3 == 0:
            holdings.append(make_row("HELD", 25., quantity=rng.choice([1., 4., 20.]),
                                     sleeve=rng.choice(["government_bond", "broad_equity"])))
        yield case(f"generated_{index:03d}", rows, budget=float(rng.randrange(20, 251)),
                   holdings=holdings, cash=rng.choice([0., 50., 200.]), risk=rng.choice(["low", "medium"]),
                   costs={**ZERO_COSTS, "fixed_fee": rng.choice([0., .01, 1., 3.]),
                          "commission_pct": rng.choice([0., .25]), "spread_bps": rng.choice([0., 10.]),
                          "fx_bps": rng.choice([0., 20.])},
                   reserve=rng.choice([0., .10, .25]), cap=rng.choice([None, .20, .45]),
                   drawdown=rng.choice([None, .10, .20]))


def source_fingerprint():
    paths = [Path(__file__), *(COMPONENT / name for name in LIVE_FILES)]
    return {str(path.relative_to(ROOT)): digest(path.read_bytes()) for path in paths}


def load_subject(path=COMPONENT, name="_purchase_search_audit_current"):
    package = ModuleType(name)
    package.__path__ = [str(path)]
    sys.modules[name] = package
    return importlib.import_module(name + ".portfolio_plan")


@contextmanager
def frozen_baseline():
    """Import actual committed prior source, not the new module under an alias."""
    with tempfile.TemporaryDirectory(prefix="investment-purchase-baseline-") as directory:
        sources = {}
        for filename in BASELINE_FILES:
            content = subprocess.check_output([
                "git", "show", f"{BASELINE_COMMIT}:custom_components/investment/{filename}",
            ], cwd=ROOT)
            (Path(directory) / filename).write_bytes(content)
            sources[filename] = digest(content)
        name = "_purchase_search_audit_baseline"
        for module_name in list(sys.modules):
            if module_name == name or module_name.startswith(name + "."):
                del sys.modules[module_name]
        yield load_subject(Path(directory), name), sources


def call_subject(subject, specification):
    return subject.finalize_purchase_plan(
        deepcopy(specification["rows"]), specification["budget"], specification["risk"],
        existing_positions=deepcopy(specification["holdings"]), existing_cash=specification["cash"],
        execution_costs=deepcopy(specification["costs"]), minimum_cash_reserve_fraction=specification["reserve"],
        max_candidate_fraction=specification["cap"], max_drawdown_loss=specification["drawdown"],
        as_of=AS_OF, bootstrap_repetitions=0,
    )


def identity(row):
    return (row.get("provider", ""), row.get("provider_id") or row.get("symbol", ""))


def valid_candidate(row):
    # The generated fixtures have no live-provider input contract. Explicitly
    # failed flags are still binding; provider identity itself is not guessed.
    return all(row.get(field) is not False for field in (
        "allocation_eligible", "instrument_identity_eligible", "data_quality_eligible"))


def unit_domains(specification, fixed_fractional=None):
    domains = []
    for index, row in enumerate(specification["rows"]):
        units, amount, price = (decimal_number(row.get(key)) for key in
                                ("suggested_units", "suggested_amount", "portfolio_price"))
        if not valid_candidate(row) or units is None or amount is None or price is None or min(units, amount) < 0 or price <= 0:
            domains.append((ZERO,))
        elif row.get("whole_units_only") is True:
            maximum = int(min(units, amount / price).to_integral_value(rounding=ROUND_FLOOR))
            domains.append(tuple(Decimal(q) for q in range(maximum + 1)))
        else:
            if fixed_fractional is None or index not in fixed_fractional:
                raise ValueError("Fractional quantities must be fixed explicitly to the verified incumbent")
            domains.append((Decimal(str(fixed_fractional[index])),))
    return domains


@lru_cache(maxsize=1024)
def _dated_history(items):
    clean = {}
    for week, value_return in items:
        ret = decimal_number(value_return)
        if ret is None or ret < -1:
            return None
        try:
            week_date = datetime.strptime(week + "-1", "%G-W%V-%u").date()
        except (TypeError, ValueError):
            return None
        if week_date > date.fromisoformat(AS_OF):
            return None
        clean[week_date] = float(ret)
    return clean


def risk_snapshot(positions, cash, drawdown_required):
    """Independent direct-wealth risk calculation for known synthetic evidence."""
    total = sum((value for _, value in positions), ZERO) + cash
    if total < 0:
        return None
    if not positions:
        return {"volatility": 0., "es": 0., "drawdown": 0., "sleeves": {}, "instruments": {}}
    maps = []
    sleeves, instruments = defaultdict(Decimal), defaultdict(Decimal)
    for row, value in positions:
        history = row.get("risk_weekly_returns")
        if not isinstance(history, dict) or not history or not valid_candidate({**row, "allocation_eligible": True}):
            return None
        clean = _dated_history(tuple(history.items()))
        if clean is None:
            return None
        weight = value / total
        maps.append((float(weight), clean))
        sleeves[row["economic_sleeve"]] += weight
        instruments[identity(row)] += weight
    dates = sorted(set.intersection(*(set(history) for _, history in maps)))
    if len(dates) < 52:
        return None
    gaps = any((second - first).days != 7 for first, second in zip(dates, dates[1:]))
    if gaps and drawdown_required:
        return None
    returns = [sum(weight * history[day] for weight, history in maps) for day in dates]
    average = sum(returns) / len(returns)
    volatility = math.sqrt(sum((value - average) ** 2 for value in returns) / len(returns) * 52)
    # Every original observation becomes 20 equal pieces. The first n pieces
    # are exactly the empirical upper 5% loss tail, including boundary mass.
    loss_pieces = sorted((-value for value in returns for _ in range(20)), reverse=True)
    es = max(0., sum(loss_pieces[:len(returns)]) / len(returns))
    wealth, peak, drawdown = 1., 1., 0.
    for value in returns:
        wealth *= 1 + value
        peak = max(peak, wealth)
        drawdown = max(drawdown, 1 - wealth / peak)
    return {"volatility": volatility, "es": es, "drawdown": None if gaps else drawdown,
            "sleeves": dict(sleeves), "instruments": dict(instruments)}


def evaluate_vector(specification, quantities):
    """Return independently computed feasibility and exact marked/cash values."""
    costs = specification["costs"]
    if not isinstance(costs, dict) or costs.get("confirmed") is not True:
        return {"feasible": not any(quantities), "principal": ZERO, "cost": ZERO, "cash_debit": ZERO}
    fixed = Decimal(str(costs["fixed_fee"]))
    rate = Decimal(str(costs["commission_pct"])) / 100 + (
        Decimal(str(costs["spread_bps"])) + Decimal(str(costs["fx_bps"]))) / 10000
    budget, outside_cash = Decimal(str(specification["budget"])), Decimal(str(specification["cash"]))
    reserve = budget * Decimal(str(specification["reserve"]))
    available = budget.quantize(CENT, rounding=ROUND_FLOOR) - reserve.quantize(CENT, rounding=ROUND_CEILING)
    additions, marked_total, debit_total = [], ZERO, ZERO
    for row, raw_units in zip(specification["rows"], quantities, strict=True):
        units = Decimal(str(raw_units))
        if units == 0:
            continue
        price = Decimal(str(row["portfolio_price"]))
        principal = units * price
        if units < 0 or not valid_candidate(row) or units > Decimal(str(row["suggested_units"])) or principal > Decimal(str(row["suggested_amount"])):
            return {"feasible": False, "reason": "purchase_ceiling_or_eligibility"}
        if row.get("whole_units_only") is True and units != units.to_integral_value():
            return {"feasible": False, "reason": "fractional_whole_lot"}
        additions.append((row, principal))
        marked_total += principal
        debit_total += (principal + fixed + principal * rate).quantize(CENT, rounding=ROUND_CEILING)
    metrics = {"principal": marked_total, "cash_debit": debit_total, "cost": debit_total - marked_total}
    if debit_total > available:
        return {"feasible": False, "reason": "cash_or_reserve", **metrics}
    holdings = []
    for row in specification["holdings"]:
        quantity = decimal_number(row.get("quantity"))
        if quantity == 0:
            continue
        price = decimal_number(row.get("portfolio_price"))
        if quantity is None or quantity < 0 or price is None or price <= 0:
            return {"feasible": not additions, "reason": "unknown_holding", **metrics}
        holdings.append((row, quantity * price))
    if not additions:
        return {"feasible": True, **metrics}
    baseline = risk_snapshot(holdings, outside_cash + budget, specification["drawdown"] is not None)
    post = risk_snapshot(holdings + additions, outside_cash + budget - debit_total, specification["drawdown"] is not None)
    if baseline is None or post is None:
        return {"feasible": False, "reason": "unknown_risk", **metrics}
    policy = POLICY[specification["risk"]]
    limits = {"volatility": policy["volatility"], "es": policy["es"], "drawdown": specification["drawdown"]}
    for key, limit in limits.items():
        if limit is not None and post[key] > max(limit, baseline[key]) + TOLERANCE:
            return {"feasible": False, "reason": key, **metrics}
    for sleeve, weight in post["sleeves"].items():
        if sleeve not in policy["sleeves"] or float(weight) > max(policy["sleeves"].get(sleeve, 0.), float(baseline["sleeves"].get(sleeve, 0.))) + TOLERANCE:
            return {"feasible": False, "reason": "sleeve", **metrics}
    if specification["cap"] is not None:
        for instrument, weight in post["instruments"].items():
            if float(weight) > max(specification["cap"], float(baseline["instruments"].get(instrument, 0.))) + TOLERANCE:
                return {"feasible": False, "reason": "instrument", **metrics}
    return {"feasible": True, **metrics}


def exhaustive_oracle(specification, fixed_fractional=None):
    domains = unit_domains(specification, fixed_fractional)
    best_score, best, feasible = None, [], 0
    examined = 0
    for quantities in itertools.product(*domains):
        examined += 1
        result = evaluate_vector(specification, quantities)
        if not result["feasible"]:
            continue
        feasible += 1
        score = (result["principal"], -result["cost"])
        if best_score is None or score > best_score:
            best_score, best = score, [quantities]
        elif score == best_score:
            best.append(quantities)
    return {"domain_size": math.prod(len(domain) for domain in domains), "examined": examined,
            "feasible_vectors": feasible, "best_principal": best_score[0] if best_score else None,
            "best_cost": -best_score[1] if best_score else None, "optimal_vectors": best}


def check_output(specification, rows, metadata):
    units = [row.get("suggested_units", 0.) for row in rows]
    result = evaluate_vector(specification, units)
    violations = []
    if not result["feasible"]:
        violations.append("independent_feasibility:" + result.get("reason", "unknown"))
    for before, after in zip(specification["rows"], rows, strict=True):
        amount = decimal_number(after.get("suggested_amount"))
        expected = Decimal(str(after.get("suggested_units", 0.))) * Decimal(str(before["portfolio_price"]))
        if amount is None or abs(amount - expected) > Decimal("1e-10"):
            violations.append("marked_principal_mismatch")
    if result.get("feasible"):
        costs = metadata["execution_costs"]
        for key, expected in (("principal", result["principal"]), ("estimated_cash_debit", result["cash_debit"]),
                              ("estimated_transaction_cost", result["cost"])):
            if abs(Decimal(str(costs[key])) - expected) > Decimal("1e-10"):
                violations.append("cost_summary:" + key)
        if abs(Decimal(str(costs["cash_remaining"])) + result["cash_debit"] - Decimal(str(specification["budget"]))) > Decimal("1e-10"):
            violations.append("cash_conservation")
    return result, violations


def serializable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [serializable(item) for item in value]
    return value


def verify_case_ledger(path):
    previous, count = None, 0
    for line in path.read_text().splitlines():
        row = json.loads(line)
        observed_hash = row.pop("record_sha256")
        if row.get("previous_sha256") != previous or digest(canonical(row)) != observed_hash:
            raise ValueError(f"Case ledger integrity failure at record {count}")
        previous, count = observed_hash, count + 1
    return count, previous


def run_audit(directory):
    directory.mkdir(parents=True, exist_ok=False)
    plan = {"schema": "whole-unit-feasibility-audit-plan-v1", "seed": SEED,
            "generated_case_count": GENERATED_CASES, "predefined_cases": [item["name"] for item in predefined_cases()],
            "generated_domain": "2-4 whole-unit candidates, 1-3 incoming units each; all draws retained",
            "baseline_commit": BASELINE_COMMIT, "policy": POLICY, "as_of": AS_OF,
            "oracle_objective": ["largest exact marked principal", "smallest exact estimated cost"],
            "tie_scope": "Equal principal/cost quantity vectors are all accepted; target-deviation tie logic is not mirrored",
            "risk_policy": "Each metric <= max(existing budget baseline metric, unchanged policy limit) + 1e-12",
            "interpretation": "Generated engineering inputs only; no historical winners, returns fitting or prospective claims"}
    (directory / "predeclared_plan.json").write_bytes(canonical(plan) + b"\n")
    fingerprint = source_fingerprint()
    started = datetime.now(timezone.utc).isoformat()
    current = load_subject()
    records, previous = [], None
    with frozen_baseline() as (baseline, baseline_hashes), (directory / "cases.jsonl").open("x") as ledger:
        for specification in itertools.chain(predefined_cases(), generated_cases()):
            record = {"name": specification["name"], "input": specification}
            try:
                beginning = time.perf_counter()
                old_rows, old_meta = call_subject(baseline, specification)
                old_runtime = time.perf_counter() - beginning
                beginning = time.perf_counter()
                new_rows, new_meta = call_subject(current, specification)
                new_runtime = time.perf_counter() - beginning
                beginning = time.perf_counter()
                oracle = exhaustive_oracle(specification)
                oracle_runtime = time.perf_counter() - beginning
                old_check, old_violations = check_output(specification, old_rows, old_meta)
                new_check, violations = check_output(specification, new_rows, new_meta)
                optimum_matches = (new_check.get("principal"), new_check.get("cost")) == (oracle["best_principal"], oracle["best_cost"])
                search = new_meta.get("purchase_search", {})
                if not optimum_matches and search.get("optimality_proven") is True:
                    violations.append("false_optimality_claim")
                if not optimum_matches and search.get("status") != "bounded":
                    violations.append("missed_small_domain_optimum")
                if new_check.get("principal", ZERO) < old_check.get("principal", ZERO):
                    violations.append("decreased_verified_principal")
                record.update(status="completed", baseline={"units": [r["suggested_units"] for r in old_rows],
                    "check": old_check, "violations": old_violations, "runtime_seconds": old_runtime},
                    current={"units": [r["suggested_units"] for r in new_rows], "check": new_check,
                    "violations": violations, "runtime_seconds": new_runtime, "search": search},
                    oracle={**oracle, "runtime_seconds": oracle_runtime}, optimum_matches=optimum_matches)
            except Exception as error:
                record.update(status="failed", error=f"{type(error).__name__}: {error}")
            payload = {**serializable(record), "previous_sha256": previous}
            previous = digest(canonical(payload))
            record = {**payload, "record_sha256": previous}
            ledger.write(canonical(record).decode() + "\n")
            ledger.flush()
            os.fsync(ledger.fileno())
            records.append(record)
            print(f"{specification['name']}: {record['status']}", flush=True)
    complete = [record for record in records if record["status"] == "completed"]
    current_violations = sum(len(record["current"]["violations"]) for record in complete)
    old_violations = sum(len(record["baseline"]["violations"]) for record in complete)
    source_valid = source_fingerprint() == fingerprint
    ledger_count, ledger_endpoint = verify_case_ledger(directory / "cases.jsonl")
    ledger_valid = ledger_count == len(records) and ledger_endpoint == previous
    improved = [record["name"] for record in complete if Decimal(record["current"]["check"].get("principal", "0")) > Decimal(record["baseline"]["check"].get("principal", "0"))]
    missed = [record["name"] for record in complete if not record["optimum_matches"]]
    summary = {"schema": "whole-unit-feasibility-audit-results-v1", "started_at_utc": started,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(), "baseline_commit": BASELINE_COMMIT,
        "baseline_source_sha256": baseline_hashes, "source_fingerprint": fingerprint,
        "source_freeze_valid": source_valid, "ledger_verified_complete": ledger_valid,
        "plan_sha256": digest((directory / "predeclared_plan.json").read_bytes()),
        "case_count": len(records), "completed_cases": len(complete), "failed_cases": len(records) - len(complete),
        "current_rule_violations": current_violations, "baseline_rule_violations": old_violations,
        "improved_principal_cases": improved, "missed_oracle_optimum_cases": missed,
        "oracle_vectors_examined": sum(record["oracle"]["examined"] for record in complete),
        "max_current_runtime_seconds": max((record["current"]["runtime_seconds"] for record in complete), default=0),
        "final_record_sha256": previous,
        "passed": len(complete) == len(records) and current_violations == 0 and not missed and source_valid and ledger_valid,
        "limitations": ["No historical or prospective investment performance is estimated",
                        "Oracle scope is the frozen generated small integer domains and synthetic histories",
                        "Same economic policy inputs; independent cash and risk arithmetic",
                        "Neither bounded search nor this audit establishes global financial optimality"]}
    (directory / "summary.json").write_bytes(canonical(summary) + b"\n")
    print(json.dumps(summary, indent=2), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="New output directory; existing evidence is never overwritten")
    args = parser.parse_args()
    if not run_audit(args.output_dir)["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
