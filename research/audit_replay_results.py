"""Independently audit retained decisions, fills and cash-flow-neutral outcomes.

This audit reads source prices and recorded traces, not model-calculated risk
labels. It verifies chronology, exact availability, order ceilings, execution
prices, cash affordability, whole units, holdings reconciliation and NAV flows.
It makes no assertion that observed historical returns establish future safety.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import defaultdict
from decimal import Decimal
import gzip
import hashlib
import json
import math
from pathlib import Path

from chronological_validation import canonical, synthetic_document


def near(a, b, *, tolerance=1e-6):
    return abs(a - b) <= tolerance * max(1, abs(a), abs(b))


def verify_run(directory: Path, data_path: Path):
    result = json.loads((directory / "results.json").read_text())
    provenance = result["provenance"]
    source = json.loads(data_path.read_bytes())
    problems, checked = [], defaultdict(int)
    if not provenance.get("source_freeze_valid"):
        problems.append("run source freeze is invalid")
    if hashlib.sha256(data_path.read_bytes()).hexdigest() != provenance["data_sha256"]:
        problems.append("source snapshot hash mismatch")
    previous = None
    ledger = []
    for index, line in enumerate((directory / "experiment_ledger.jsonl").read_text().splitlines()):
        row = json.loads(line)
        digest = row.pop("record_sha256")
        if row["previous_record_sha256"] != previous or hashlib.sha256(canonical(row)).hexdigest() != digest:
            problems.append(f"ledger chain invalid at record {index}")
        previous = digest
        ledger.append(row)
    if previous != provenance["ledger_last_sha256"] or len(ledger) != len(result["results"]):
        problems.append("ledger endpoint/count mismatch")

    for run in result["results"]:
        label, settings = run["label"], run["parameters"]
        name = run["trial_id"]

        def check(condition, message):
            if not condition:
                problems.append(name + ": " + message)

        if run["status"] != "completed":
            problems.append(name + ": failed trial retained: " + run.get("error", ""))
            continue
        checked["trials"] += 1
        document = synthetic_document(label.removeprefix("synthetic_")) if label.startswith("synthetic_") else source
        check(hashlib.sha256(canonical(document)).hexdigest() == run["data_sha256"], "trial dataset hash differs")
        raw = gzip.decompress((directory / run["trace_file"]).read_bytes())
        check(hashlib.sha256(raw).hexdigest() == run["trace_sha256"], "trace hash differs")
        trace = json.loads(raw)
        assets = document["assets"]
        by_date = {symbol: {b["date"]: b["close"] for b in asset["bars"] if b["date"] >= asset["inception"]} for symbol, asset in assets.items()}
        dates = {symbol: sorted(values) for symbol, values in by_date.items()}

        def price(symbol, day):
            offset = bisect_right(dates[symbol], day) - 1
            if offset < 0:
                raise ValueError("valuation before instrument inception")
            return by_date[symbol][dates[symbol][offset]]

        orders = {row["execution_session"]: row for row in trace["trace"]}
        quantities = dict(settings["initial_positions"])
        cash = Decimal(str(settings["initial_cash"]))
        initial = run["result"]["metrics"]["initial_capital"]
        issued = initial
        last_nav = 1.0
        peak = 1.0
        max_decline = 0.0
        total_deposits = 0.0
        total_fees = 0.0
        all_trade_count = 0
        seen_months = set()
        for point in trace["daily_accounting"]:
            day = point["date"]
            checked["daily_accounting_points"] += 1
            pre_flow_wealth = float(cash) + sum(q * price(symbol, day) for symbol, q in quantities.items())
            pre_flow_nav = pre_flow_wealth / issued if issued else 1.0
            flow = point["contribution"]
            expected_flow = settings["contribution"] if day[:7] not in seen_months else 0.0
            seen_months.add(day[:7])
            check(flow == expected_flow, "monthly external contribution differs")
            if flow:
                total_deposits += flow
                issued += flow / pre_flow_nav
                cash += Decimal(str(flow))
                decision = orders.get(day)
                check(decision is not None, "missing monthly decision")
                if decision is None:
                    continue
                checked["decisions"] += 1
                check(decision["decision_session"] < day, "decision saw execution session")
                candidate = {c["symbol"]: c for c in decision["candidate_history"]}
                caps = {c["symbol"]: c["amount"] for c in decision["orders"]}
                for history in candidate.values():
                    check(history["last"] <= decision["decision_session"], "future observation in history")
                    check(history["first"] >= assets[history["symbol"]]["inception"], "history borrows pre-inception prices")
                for trade in decision["trades"]:
                    checked["fills"] += 1
                    all_trade_count += 1
                    symbol = trade["symbol"]
                    check(symbol in candidate, "filled instrument had no candidate evidence")
                    if symbol in candidate:
                        check(candidate[symbol]["weekly_count"] >= 52, "filled instrument had insufficient history")
                    check(day in by_date[symbol], "fill used a missing execution observation")
                    check(day >= assets[symbol]["inception"], "fill before exact inception")
                    check(near(trade["price"], by_date[symbol].get(day, -1)), "fill price differs from recorded source")
                    check(trade["units"] > 0 and trade["principal"] > 0, "nonpositive fill")
                    check(trade["principal"] <= caps.get(symbol, 0) + 1e-8, "fill exceeds approved currency amount")
                    if settings["whole_units"]:
                        check(trade["units"] == int(trade["units"]), "fractional fill in whole-unit scenario")
                    exact_principal = Decimal(str(trade["units"])) * Decimal(str(trade["price"]))
                    debit_principal = exact_principal.quantize(Decimal(".01"), rounding="ROUND_UP")
                    check(near(float(debit_principal), trade["principal"], tolerance=1e-8), "principal cash rounding differs")
                    expected_fee = (Decimal(str(settings["fixed_fee"])) + debit_principal * Decimal(str(settings["commission_bps"] + settings["execution_bps"])) / Decimal(10000)).quantize(Decimal(".01"), rounding="ROUND_UP")
                    check(near(float(expected_fee), trade["fees"], tolerance=1e-8), "configured fee differs")
                    check(near(trade["principal"] + trade["fees"], trade["cash_debit"]), "cash debit excludes a fee")
                    cash -= Decimal(str(trade["cash_debit"]))
                    check(cash >= 0, "purchases overspent cash")
                    quantities[symbol] = quantities.get(symbol, 0) + trade["units"]
                    total_fees += trade["fees"] + trade["rounding_cost"]
            check(near(float(cash), point["cash"], tolerance=1e-8), "cash carry does not reconcile")
            wealth = float(cash) + sum(q * price(symbol, day) for symbol, q in quantities.items())
            check(near(wealth, point["wealth"]), "wealth does not reconcile with held units")
            nav = wealth / issued if issued else 1.0
            check(near(nav, point["nav"]), "deposit-neutral portfolio NAV differs")
            last_nav = nav
            peak = max(peak, nav)
            max_decline = max(max_decline, 1 - nav / peak)
        metrics = run["result"]["metrics"]
        check(near(metrics["contributions"], total_deposits), "aggregate deposits differ")
        check(near(metrics["total_execution_costs"], total_fees), "aggregate costs differ")
        check(near(metrics["max_unitized_drawdown"], max_decline), "drawdown is not unitized")
        check(near(metrics["time_weighted_total_return_after_costs"], last_nav - 1), "unitized total return differs")
        check(metrics["trade_count"] == all_trade_count, "aggregate trade count differs")
        check(set(quantities) == set(run["result"]["ending_positions"]) and all(near(q, run["result"]["ending_positions"][s]) for s, q in quantities.items()), "ending holdings differ")
    return {"schema": "ha-investment-replay-audit-v1", "run_id": provenance["run_id"],
            "checked": dict(checked), "violations": problems, "passed": not problems,
            "scope": "source/trace/ledger identity, time ordering, exact inception, minimum history, next-session observed fills, approved currency ceilings, all costs, whole units, nonnegative cash, held positions and deposit-neutral NAV; not a future-performance guarantee"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    result = verify_run(args.run_dir, args.data)
    output = args.run_dir / "independent_accounting_audit.json"
    with output.open("x") as stream:
        stream.write(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps(result))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
