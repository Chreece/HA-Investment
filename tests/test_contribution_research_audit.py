"""Independent adversarial checks for the retained contribution-study ledger."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from research import audit_contribution_results as auditor
from research.chronological_validation import synthetic_document
from research.contribution_candidates import ledoit_wolf_covariance
from research.contribution_validation import Experiment, replay


def source_with_known_account_returns():
    """A flat warmup, then two explicit 10 percent gains on known sessions."""
    rows = []
    day = date(2020, 1, 2)
    while day <= date(2021, 4, 30):
        if day.weekday() < 5:
            value = 100.0 if day < date(2021, 2, 2) else 110.0 if day < date(2021, 3, 1) else 121.0
            stamp = int(datetime.combine(day, datetime.min.time(), timezone.utc).timestamp()) + 12 * 3600
            rows.append(dict(date=day.isoformat(), ts=stamp, close=value, adjusted_close=value))
        day += timedelta(days=1)
    return {"schema": "independent-audit-fixture", "kind": "synthetic_adversarial_scenario",
            "currency": "EUR", "assets": {"EQUITY": {
                "name": "Synthetic MSCI World Broad Equity", "category": "etf", "currency": "EUR",
                "sleeve": "broad_equity", "inception": "2020-01-02", "bars": rows}}}


@pytest.fixture
def audited_trace():
    document = synthetic_document("flat_cost_drag", sessions=430)
    parameters = Experiment(start="2021-02-01", end="2021-03-15", development_end="2021-02-15")
    trace = replay(document, parameters)
    return document, trace


def test_independent_audit_accepts_reconciled_accounting(audited_trace):
    document, trace = audited_trace
    result = auditor.audit_trial(document, trace)
    assert result["passed"], result["violations"]
    assert result["checked"]["daily_accounting_points"] == 43
    assert result["checked"]["decisions"] == 2
    assert result["checked"]["fills"] > 0
    assert result["checked"]["independent_historical_plan_risk_checks"] >= 2


@pytest.mark.parametrize("corruption,expected", [
    (lambda trial: trial["trace"][0].__setitem__("quote_budget", 200.0), "budget"),
    (lambda trial: trial["daily_accounting"][0].__setitem__("contribution", 200.0), "contribution"),
    (lambda trial: trial["daily_accounting"][1].__setitem__("cash", 999.0), "cash ledger"),
    (lambda trial: trial["daily_accounting"][1].__setitem__("nav", 1.2345), "NAV"),
    (lambda trial: trial["daily_accounting"].pop(1), "missing, duplicate or extra dates"),
    (lambda trial: trial["trace"][0]["trades"][0].__setitem__("price", 999.0), "execution price"),
    (lambda trial: trial["trace"][0]["trades"][0].__setitem__("cash_debit", 999.0), "cash debit"),
    (lambda trial: trial["trace"][0]["trades"][0].__setitem__("fees", 999.0), "fees"),
    (lambda trial: trial["trace"][0]["trades"][0].__setitem__("rounding_cost", .05), "rounding"),
    (lambda trial: trial["trace"][0]["trades"][0].__setitem__("approved_units", 999.0), "approval limits"),
    (lambda trial: trial["trace"][0]["candidate_history"][0].__setitem__("weekly_count", 999), "history count"),
    (lambda trial: trial["trace"][0].__setitem__("calendar_assets", []), "execution universe"),
    (lambda trial: trial["trace"][0]["quote_guard"]["portfolio_risk"].__setitem__("existing_cash", 50.0), "old account"),
    (lambda trial: trial["metrics"].__setitem__("max_unitized_drawdown", .99), "max_unitized_drawdown"),
])
def test_independent_audit_detects_retained_ledger_corruption(audited_trace, corruption, expected):
    document, trace = audited_trace
    tampered = deepcopy(trace)
    corruption(tampered)
    result = auditor.audit_trial(document, tampered)
    assert not result["passed"]
    assert any(expected in message for message in result["violations"]), result["violations"]


def test_reduced_approved_units_cannot_hide_in_a_self_reported_fill(audited_trace):
    document, trace = audited_trace
    tampered = deepcopy(trace)
    fill = tampered["trace"][0]["trades"][0]
    approved = next(order for order in tampered["trace"][0]["orders"] if order["symbol"] == fill["symbol"])
    approved["suggested_units"] = fill["units"] / 2
    fill["approved_units"] = approved["suggested_units"]
    report = auditor.audit_trial(document, tampered)
    assert any("increased quote-approved units" in message for message in report["violations"])


def test_cash_only_existing_position_and_midnight_flows_have_closed_form_nav():
    document = source_with_known_account_returns()
    trace = replay(document, Experiment(strategy="cash_only", start="2021-02-01", end="2021-03-15",
                                       development_end="2021-02-28", initial_cash=100.0,
                                       initial_positions=(("EQUITY", 1.0),)))
    # Initial 200 + February 100 = 300; gain 10 -> 310. March 100 arrives before
    # that day's 11 gain, so return factors are 310/300 then 421/410.
    expected_return = float(Fraction(31, 30) * Fraction(421, 410) - 1)
    assert trace["metrics"]["ending_nominal_wealth"] == 421.0
    assert trace["metrics"]["initial_capital"] == 200.0
    assert trace["metrics"]["contributions"] == 200.0
    assert trace["metrics"]["time_weighted_total_return_after_costs"] == pytest.approx(expected_return, abs=1e-12)
    assert trace["metrics"]["trade_count"] == 0
    report = auditor.audit_trial(document, trace)
    assert report["passed"], report["violations"]


def test_no_common_session_keeps_external_contribution_as_cash():
    document = synthetic_document("flat_cost_drag", sessions=430)
    document["assets"]["BOND"]["bars"] = [row for row in document["assets"]["BOND"]["bars"]
                                              if not row["date"].startswith("2021-02")]
    trace = replay(document, Experiment(start="2021-02-01", end="2021-02-28"))
    assert trace["metrics"]["contributions"] == 100.0
    assert trace["metrics"]["ending_cash"] == 100.0
    assert trace["metrics"]["trade_count"] == 0
    report = auditor.audit_trial(document, trace)
    assert report["passed"], report["violations"]


def test_owned_exclusion_is_checked_against_reconstructed_prior_quantities():
    document = synthetic_document("flat_cost_drag", sessions=430)
    trace = replay(document, Experiment(start="2021-02-01", end="2021-03-15", existing_instruments="exclude"))
    assert trace["trace"][0]["trades"]
    assert trace["trace"][1]["trades"] == []
    report = auditor.audit_trial(document, trace)
    assert report["passed"], report["violations"]
    tampered = deepcopy(trace)
    symbol = trace["trace"][0]["trades"][0]["symbol"]
    tampered["parameters"]["initial_positions"] = ((symbol, 1.0),)
    report = auditor.audit_trial(document, tampered)
    assert any("already-owned instrument" in message for message in report["violations"])


def test_old_account_balance_cannot_change_new_contribution_orders():
    document = synthetic_document("flat_cost_drag", sessions=430)
    common = dict(start="2021-02-01", end="2021-03-15", existing_instruments="allow")
    empty = replay(document, Experiment(**common))
    funded = replay(document, Experiment(**common, initial_cash=500.0, initial_positions=(("EQUITY", 50.0),)))
    for before, after in zip(empty["trace"], funded["trace"], strict=True):
        for field in ("candidate_history", "orders", "trades", "quote_guard", "fill_guard"):
            assert before[field] == after[field]
    assert funded["metrics"]["ending_nominal_wealth"] > empty["metrics"]["ending_nominal_wealth"]
    assert auditor.audit_trial(document, funded)["passed"]


def source_with_daily_crypto():
    document = synthetic_document("flat_cost_drag", sessions=430)
    rows, day = [], date(2020, 1, 2)
    end = date.fromisoformat(document["assets"]["EQUITY"]["bars"][-1]["date"])
    while day <= end:
        value = 20.0 + len(rows) / 100
        stamp = int(datetime.combine(day, datetime.min.time(), timezone.utc).timestamp()) + 12 * 3600
        rows.append(dict(date=day.isoformat(), ts=stamp, close=value, adjusted_close=value))
        day += timedelta(days=1)
    document["assets"]["CRYPTO"] = {
        "name": "Synthetic Bitcoin", "category": "crypto", "currency": "EUR", "sleeve": "crypto",
        "inception": "2020-01-02", "bars": rows,
    }
    return document


@pytest.fixture
def mixed_event_trace():
    document = source_with_daily_crypto()
    config = Experiment(strategy="equal_sleeves", contribution=1000.0, risk="high",
                        start="2021-02-01", end="2021-02-01")
    return document, replay(document, config)


def test_later_crypto_price_cannot_change_earlier_exchange_fills(mixed_event_trace):
    document, before = mixed_event_trace
    changed = deepcopy(document)
    later = next(row for row in changed["assets"]["CRYPTO"]["bars"] if row["date"] == "2021-02-01")
    later["close"] *= 10
    later["adjusted_close"] *= 10
    after = replay(changed, Experiment(**before["parameters"]))
    original_decision, changed_decision = before["trace"][0], after["trace"][0]
    assert original_decision["orders"] == changed_decision["orders"]
    earlier = lambda decision: [trade for trade in decision["trades"] if trade["stage"] == "exchange_close"]
    later_fills = lambda decision: [trade for trade in decision["trades"] if trade["stage"] == "crypto_close"]
    assert earlier(original_decision)
    assert earlier(original_decision) == earlier(changed_decision)
    assert later_fills(original_decision)
    assert later_fills(original_decision) != later_fills(changed_decision)
    assert auditor.audit_trial(document, before)["passed"]
    report = auditor.audit_trial(changed, after)
    assert report["passed"], report["violations"]


@pytest.mark.parametrize("corruption,expected", [
    (lambda decision: decision["fill_guard"]["stages"].reverse(), "stage order"),
    (lambda decision: decision["fill_guard"]["stages"][1].__setitem__("committed_units_before", {}), "earlier fills"),
    (lambda decision: decision["fill_guard"]["stages"][1].__setitem__("cash_before", 1000.0), "stage reused"),
    (lambda decision: decision["fill_guard"]["stages"][0].__setitem__("execution_timestamp", "2021-02-01T23:59:59+00:00"), "event timestamp"),
    (lambda decision: decision["fill_guard"]["portfolio_risk"].__setitem__("already_filled_in_this_contribution", {}), "committed units"),
    (lambda decision: decision.__setitem__("pre_flow_nav", 1.25), "midnight flow"),
])
def test_independent_audit_rejects_stage_or_flow_chronology_corruption(mixed_event_trace, corruption, expected):
    document, trace = mixed_event_trace
    tampered = deepcopy(trace)
    corruption(tampered["trace"][0])
    report = auditor.audit_trial(document, tampered)
    assert not report["passed"]
    assert any(expected in message for message in report["violations"]), report["violations"]


def test_completed_month_audit_rejects_partial_month_signal():
    document = source_with_daily_crypto()
    trace = replay(document, Experiment(strategy="sma10_equal_sleeves", start="2021-02-01", end="2021-03-15"))
    report = auditor.audit_trial(document, trace)
    assert report["passed"], report["violations"]
    assert report["checked"]["completed_month_trend_checks"] > 0
    tampered = deepcopy(trace)
    signal = tampered["trace"][0]["policy"]["transformations"]["candidate_diagnostics"][0]["trend"]
    signal["observations"][-1]["date"] = "2021-02-01"
    report = auditor.audit_trial(document, tampered)
    assert any("completed source months" in message for message in report["violations"])


def test_whole_exchange_lots_preserve_nonzero_reserve_across_both_stages():
    document = source_with_daily_crypto()
    trace = replay(document, Experiment(
        strategy="equal_sleeves", contribution=1000.0, risk="high", start="2021-02-01", end="2021-02-01",
        whole_unit_categories=("etf",), minimum_cash_reserve_fraction=.2, max_candidate_fraction=.3,
    ))
    decision = trace["trace"][0]
    assert any(trade["stage"] == "exchange_close" for trade in decision["trades"])
    assert any(trade["stage"] == "crypto_close" for trade in decision["trades"])
    assert all(trade["units"].is_integer() for trade in decision["trades"] if trade["stage"] == "exchange_close")
    assert decision["new_contribution_cash_remaining"] >= 200.0
    report = auditor.audit_trial(document, trace)
    assert report["passed"], report["violations"]
    tampered = deepcopy(trace)
    tampered["parameters"]["minimum_cash_reserve_fraction"] = .95
    report = auditor.audit_trial(document, tampered)
    assert any("reserve" in message for message in report["violations"])


def test_shrinkage_matches_independent_fractional_matrix_oracle():
    # Centered outer-product oracle: S=1e-4*[[5/2,2],[2,5/2]], target=muI.
    # Numerator=9/8*1e-8, denominator=8*1e-8, giving intensity 9/64.
    covariance, details = ledoit_wolf_covariance([[.01, .02], [.02, .01], [-.01, -.02], [-.02, -.01]])
    assert details["shrinkage"] == pytest.approx(float(Fraction(9, 64)), abs=1e-14)
    assert covariance[0][0] == pytest.approx(.00025, abs=1e-15)
    assert covariance[1][1] == pytest.approx(.00025, abs=1e-15)
    assert covariance[0][1] == pytest.approx(float(Fraction(55, 32)) / 10000, abs=1e-15)
    assert covariance[1][0] == covariance[0][1]


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _write_fake_frozen_run(directory, document, trace):
    directory.mkdir()
    data_path = directory.parent / "data.json"
    data_path.write_bytes(_canonical(document))
    dataset_digest = _digest(data_path.read_bytes())
    (directory.parent / "research").mkdir()
    frozen_source = directory.parent / "research/frozen_source.py"
    frozen_source.write_text("# frozen source fixture\n")
    parameters = deepcopy(trace["parameters"])
    strategy = parameters.pop("strategy")
    plan = {"study_id": "audit-fixture", "baseline_commit": "fixed-fixture-commit", "dataset_sha256": dataset_digest,
            "strategies": [strategy], "defaults": parameters,
            "scenarios": [{"id": "observed_primary", "dataset": "observed", "parameters": {}}]}
    (directory / "predeclared_plan.json").write_bytes(_canonical(plan))
    (directory / "datasets").mkdir()
    source_name = "datasets/" + dataset_digest + ".json.gz"
    (directory / source_name).write_bytes(gzip.compress(data_path.read_bytes(), mtime=0))
    trial_id = "001-observed_primary-" + strategy
    trial = {"trial_id": trial_id, "scenario": "observed_primary", "strategy": strategy,
             "parameters": trace["parameters"], "dataset_sha256": dataset_digest}
    trials = [trial]
    (directory / "predeclared_trials.json").write_bytes(_canonical(trials))
    manifest = {"study_id": plan["study_id"], "baseline_commit": plan["baseline_commit"],
                "frozen_at_utc": "2000-01-01T00:00:00+00:00",
                "plan_sha256": _digest(_canonical(plan)), "trials_sha256": _digest(_canonical(trials)),
                "files": {"research/frozen_source.py": _digest(frozen_source.read_bytes())},
                "datasets": {"data.json": dataset_digest},
                "scenario_datasets": {"observed_primary": {"file": source_name, "sha256": dataset_digest}}}
    manifest_raw = _canonical(manifest)
    (directory / "source_manifest.json").write_bytes(manifest_raw)
    raw = _canonical(trace)
    (directory / "trace.json.gz").write_bytes(gzip.compress(raw, mtime=0))
    case = {**trial,
            "status": "completed", "trace_file": "trace.json.gz", "trace_sha256": _digest(raw),
            "result": {key: trace[key] for key in ("metrics", "development", "evaluation", "ending_positions", "execution_contract")}}
    record = {**case, "previous_record_sha256": None}
    record["record_sha256"] = _digest(_canonical(record))
    (directory / "results.json").write_bytes(_canonical({
        "study_id": plan["study_id"], "source_manifest_sha256": _digest(manifest_raw),
        "ledger_last_sha256": record["record_sha256"], "trial_count": 1, "completed_count": 1,
        "failed_count": 0, "source_freeze_valid": True, "cases": [case],
    }))
    (directory / "experiment_ledger.jsonl").write_bytes(_canonical(record) + b"\n")
    (directory / "attempts").mkdir()
    (directory / "attempts" / (trial_id + ".001.started.json")).write_bytes(_canonical({
        "trial": trial, "attempt_number": 1, "started_at_utc": "2000-01-01T00:00:01+00:00",
        "reason": "initial_attempt",
    }))
    (directory / "attempts" / (trial_id + ".result.json")).write_bytes(_canonical({
        "record": case, "record_sha256": _digest(_canonical(case)), "attempt_number": 1,
    }))
    return data_path


def test_frozen_run_verifies_plan_data_ledger_and_source_files(tmp_path, monkeypatch, audited_trace):
    document, trace = audited_trace
    run_dir = tmp_path / "run"
    data_path = _write_fake_frozen_run(run_dir, document, trace)
    monkeypatch.setattr(auditor, "REPO", tmp_path)
    report = auditor.verify_run(run_dir, data_path)
    assert report["passed"], report["violations"]
    (tmp_path / "research/frozen_source.py").write_text("# modified after freeze\n")
    report = auditor.verify_run(run_dir, data_path)
    assert not report["passed"]
    assert any("source file changed" in message for message in report["violations"])


def test_frozen_run_rejects_a_missing_predeclared_trial(tmp_path, monkeypatch, audited_trace):
    document, trace = audited_trace
    run_dir = tmp_path / "run"
    data_path = _write_fake_frozen_run(run_dir, document, trace)
    monkeypatch.setattr(auditor, "REPO", tmp_path)
    result = json.loads((run_dir / "results.json").read_bytes())
    result["cases"] = []
    (run_dir / "results.json").write_bytes(_canonical(result))
    report = auditor.verify_run(run_dir, data_path)
    assert not report["passed"]
    assert any("predeclared" in message for message in report["violations"])


def test_frozen_run_rejects_modified_trace_even_if_accounting_stays_plausible(tmp_path, monkeypatch, audited_trace):
    document, trace = audited_trace
    run_dir = tmp_path / "run"
    data_path = _write_fake_frozen_run(run_dir, document, trace)
    monkeypatch.setattr(auditor, "REPO", tmp_path)
    changed = deepcopy(trace)
    changed["execution_contract"]["prospective_evidence"] = True
    (run_dir / "trace.json.gz").write_bytes(gzip.compress(_canonical(changed), mtime=0))
    report = auditor.verify_run(run_dir, data_path)
    assert not report["passed"]
    assert any("trace hash" in message for message in report["violations"])


@pytest.mark.parametrize("filename,key,value,expected", [
    ("predeclared_plan.json", "study_id", "changed-after-freeze", "plan hash"),
    ("source_manifest.json", "baseline_commit", "changed-after-freeze", "manifest hash"),
    ("results.json", "failed_count", 5, "completed/failed"),
    ("results.json", "ledger_last_sha256", "wrong-endpoint", "ledger endpoint"),
])
def test_frozen_run_rejects_provenance_or_status_corruption(tmp_path, monkeypatch, audited_trace,
                                                          filename, key, value, expected):
    document, trace = audited_trace
    run_dir = tmp_path / "run"
    data_path = _write_fake_frozen_run(run_dir, document, trace)
    monkeypatch.setattr(auditor, "REPO", tmp_path)
    artifact = json.loads((run_dir / filename).read_bytes())
    artifact[key] = value
    (run_dir / filename).write_bytes(_canonical(artifact))
    report = auditor.verify_run(run_dir, data_path)
    assert not report["passed"]
    assert any(expected in message for message in report["violations"]), report["violations"]


def test_frozen_run_rejects_worker_outcome_missing_from_durable_ledger(tmp_path, monkeypatch, audited_trace):
    document, trace = audited_trace
    run_dir = tmp_path / "run"
    data_path = _write_fake_frozen_run(run_dir, document, trace)
    monkeypatch.setattr(auditor, "REPO", tmp_path)
    path = next((run_dir / "attempts").glob("*.result.json"))
    envelope = json.loads(path.read_bytes())
    envelope["record"]["status"] = "failed"
    envelope["record_sha256"] = _digest(_canonical(envelope["record"]))
    path.write_bytes(_canonical(envelope))
    report = auditor.verify_run(run_dir, data_path)
    assert not report["passed"]
    assert any("durable worker result differs" in message for message in report["violations"]), report["violations"]
