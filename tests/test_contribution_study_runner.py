"""Durability and tamper checks for the predeclared experiment ledger."""
import gzip
import json
from dataclasses import asdict

import pytest

from research.chronological_validation import append_ledger_record, canonical
from research import run_contribution_study as study


def case(index):
    return {"trial_id": f"{index:03d}-fixture", "scenario": "fixture",
            "strategy": "baseline_current", "parameters": {"strategy": "baseline_current"},
            "dataset_sha256": "fixture-data-identity"}


def completed(declared, value=1):
    raw = canonical({"fixture": value})
    result = {**declared, "status": "completed", "trace_file": declared["trial_id"] + ".json.gz",
              "trace_sha256": study.digest(raw), "result": {"fixture": value}}
    return result, gzip.compress(raw, mtime=0)


def retain(directory, record, compressed=None, previous=None):
    if compressed is not None:
        (directory / record["trace_file"]).write_bytes(compressed)
    return append_ledger_record(directory / "experiment_ledger.jsonl", record, previous)


class SerialExecutor:
    """Exercise the real controller without spawning financial trial workers."""
    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def map(self, function, *iterables, **kwargs):
        return map(function, *iterables)


def prepare(tmp_path, monkeypatch, count=2):
    trials = [case(index) for index in range(1, count + 1)]
    plan = {"study_id": "fixture-study", "scientific_contract": {"production_promotion": False}}
    (tmp_path / "experiment_ledger.jsonl").write_text("")
    (tmp_path / "source_manifest.json").write_text("{}\n")
    monkeypatch.setattr(study, "verify_freeze", lambda directory: (plan, trials, {}))
    monkeypatch.setattr(study, "ProcessPoolExecutor", SerialExecutor)
    return trials


def test_ledger_rejects_different_declared_order(tmp_path):
    (tmp_path / "experiment_ledger.jsonl").write_text("")
    record = {**case(2), "status": "failed", "error": "retained fixture failure"}
    retain(tmp_path, record)
    with pytest.raises(ValueError, match="declared order"):
        study._read_ledger(tmp_path, [case(1), case(2)])


def test_retained_trace_tampering_is_detected(tmp_path):
    (tmp_path / "experiment_ledger.jsonl").write_text("")
    record, compressed = completed(case(1))
    retain(tmp_path, record, compressed)
    (tmp_path / record["trace_file"]).write_bytes(gzip.compress(canonical({"fixture": 999}), mtime=0))
    with pytest.raises(ValueError, match="trace changed"):
        study._read_ledger(tmp_path, [case(1)])


def test_resume_never_retries_a_retained_failed_attempt(tmp_path, monkeypatch):
    trials = prepare(tmp_path, monkeypatch)
    retain(tmp_path, {**trials[0], "status": "failed", "error": "known failure"})
    seen = []

    def worker(plan, declared, directory):
        seen.append(declared["trial_id"])
        return completed(declared)

    monkeypatch.setattr(study, "_trial_worker", worker)
    summary = study.run(tmp_path, workers=1)
    assert seen == [trials[1]["trial_id"]]
    assert summary["completed_count"] == 1
    assert summary["failed_count"] == 1
    results = json.loads((tmp_path / "results.json").read_text())
    assert results["cases"][0]["error"] == "known failure"
    reused = study.run(tmp_path, workers=1)
    assert reused["failed_count"] == 1
    assert reused["completed_count"] == 1


def test_byte_identical_orphan_is_reconciled_and_kept(tmp_path, monkeypatch):
    trials = prepare(tmp_path, monkeypatch, count=1)
    record, compressed = completed(trials[0])
    (tmp_path / record["trace_file"]).write_bytes(compressed)
    monkeypatch.setattr(study, "_trial_worker", lambda *args: (record, compressed))
    summary = study.run(tmp_path, workers=1)
    assert summary["completed_count"] == 1
    assert (tmp_path / record["trace_file"]).read_bytes() == compressed
    assert len((tmp_path / "experiment_ledger.jsonl").read_text().splitlines()) == 1


def test_different_orphan_is_not_overwritten(tmp_path, monkeypatch):
    trials = prepare(tmp_path, monkeypatch, count=1)
    record, compressed = completed(trials[0])
    old = gzip.compress(canonical({"fixture": "a different attempt"}), mtime=0)
    (tmp_path / record["trace_file"]).write_bytes(old)
    monkeypatch.setattr(study, "_trial_worker", lambda *args: (record, compressed))
    with pytest.raises(ValueError, match="Orphan trace differs"):
        study.run(tmp_path, workers=1)
    assert (tmp_path / record["trace_file"]).read_bytes() == old
    assert (tmp_path / "experiment_ledger.jsonl").read_text() == ""


def test_completed_run_checks_summaries_before_reusing_results(tmp_path, monkeypatch):
    trials = prepare(tmp_path, monkeypatch, count=1)
    monkeypatch.setattr(study, "_trial_worker", lambda plan, declared, directory: completed(declared))
    study.run(tmp_path, workers=1)
    assert study.run(tmp_path, workers=1)["status"] == "already_completed"
    path = tmp_path / "results.json"
    results = json.loads(path.read_text())
    results["cases"][0]["result"]["fixture"] = 999
    path.write_bytes(canonical(results))
    with pytest.raises(ValueError, match="summaries disagree"):
        study.run(tmp_path, workers=1)


def test_source_change_after_trials_prevents_completion_claim(tmp_path, monkeypatch):
    trials = prepare(tmp_path, monkeypatch, count=1)
    original = study.verify_freeze
    calls = []

    def check(directory):
        calls.append(True)
        if len(calls) > 1:
            raise ValueError("Frozen code changed")
        return original(directory)

    monkeypatch.setattr(study, "verify_freeze", check)
    monkeypatch.setattr(study, "_trial_worker", lambda plan, declared, directory: completed(declared))
    with pytest.raises(ValueError, match="Frozen code changed"):
        study.run(tmp_path, workers=1)
    assert not (tmp_path / "results.json").exists()
    assert len((tmp_path / "experiment_ledger.jsonl").read_text().splitlines()) == 1


def durable_fixture(tmp_path, monkeypatch):
    from research import contribution_validation as replay_module
    parameters = asdict(replay_module.Experiment())
    document = {"kind": "synthetic_adversarial_scenario", "currency": "EUR", "assets": {
        "TEST": {"name": "Synthetic instrument", "category": "etf", "currency": "EUR",
                 "inception": "2020-01-01", "bars": [
                     {"date": "2020-01-01", "ts": 1577836800, "close": 100.0, "adjusted_close": 100.0}
                 ]}
    }}
    raw = canonical(document)
    declared = {**case(1), "parameters": parameters, "dataset_sha256": study.digest(raw)}
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / (declared["dataset_sha256"] + ".json.gz")).write_bytes(gzip.compress(raw, mtime=0))
    result = {"parameters": parameters, "metrics": {"test_value": 1},
              "development": {}, "evaluation": {}, "ending_positions": {}, "execution_contract": {}}
    monkeypatch.setattr(replay_module, "replay", lambda *args, **kwargs: result)
    return replay_module, declared, result


def test_completed_worker_is_durable_before_parent_ledger_consumes_it(tmp_path, monkeypatch):
    replay_module, declared, _ = durable_fixture(tmp_path, monkeypatch)
    first, compressed = study._trial_worker({}, declared, str(tmp_path))
    assert first["status"] == "completed"
    assert not (tmp_path / "experiment_ledger.jsonl").exists()
    assert (tmp_path / first["trace_file"]).read_bytes() == compressed

    def forbidden(*args, **kwargs):
        pytest.fail("Unledgered completed worker was evaluated again")

    monkeypatch.setattr(replay_module, "replay", forbidden)
    second, second_compressed = study._trial_worker({}, declared, str(tmp_path))
    assert canonical(second) == canonical(first)
    assert second_compressed == compressed
    assert len(list((tmp_path / "attempts").glob("*.started.json"))) == 1


def test_failed_worker_is_reused_without_silently_retrying(tmp_path, monkeypatch):
    replay_module, declared, _ = durable_fixture(tmp_path, monkeypatch)

    def failure(*args, **kwargs):
        raise ValueError("Deliberately retained failed attempt")

    monkeypatch.setattr(replay_module, "replay", failure)
    first, compressed = study._trial_worker({}, declared, str(tmp_path))
    assert first["status"] == "failed" and compressed is None

    def forbidden(*args, **kwargs):
        pytest.fail("Failed worker was silently retried")

    monkeypatch.setattr(replay_module, "replay", forbidden)
    second, _ = study._trial_worker({}, declared, str(tmp_path))
    assert canonical(second) == canonical(first)
    assert len(list((tmp_path / "attempts").glob("*.started.json"))) == 1


def test_unfinished_worker_resume_retains_both_started_attempts(tmp_path, monkeypatch):
    replay_module, declared, result = durable_fixture(tmp_path, monkeypatch)

    def interruption(*args, **kwargs):
        raise KeyboardInterrupt("Process interrupted before any outcome")

    monkeypatch.setattr(replay_module, "replay", interruption)
    with pytest.raises(KeyboardInterrupt):
        study._trial_worker({}, declared, str(tmp_path))
    assert not list((tmp_path / "attempts").glob("*.result.json"))
    monkeypatch.setattr(replay_module, "replay", lambda *args, **kwargs: result)
    record, _ = study._trial_worker({}, declared, str(tmp_path))
    assert record["status"] == "completed"
    attempts = sorted((tmp_path / "attempts").glob("*.started.json"))
    assert len(attempts) == 2
    assert json.loads(attempts[1].read_text())["reason"] == "resume_unfinished_attempt_with_unchanged_parameters"


def test_corrupt_durable_worker_is_not_silently_recomputed(tmp_path, monkeypatch):
    _, declared, _ = durable_fixture(tmp_path, monkeypatch)
    study._trial_worker({}, declared, str(tmp_path))
    path = tmp_path / "attempts" / (declared["trial_id"] + ".result.json")
    envelope = json.loads(path.read_text())
    envelope["record"]["result"]["metrics"]["test_value"] = 999
    path.write_bytes(canonical(envelope))
    with pytest.raises(ValueError, match="checksum changed"):
        study._trial_worker({}, declared, str(tmp_path))
    assert len(list((tmp_path / "attempts").glob("*.started.json"))) == 1
