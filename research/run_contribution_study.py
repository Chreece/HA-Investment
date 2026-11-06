"""Freeze, run and verify the prespecified contribution-only research suite.

The source/plan/data freeze precedes all empirical outcomes. Every attempted
trial, including a failure, is retained in an ordered hash-chain ledger. Runs
can resume after interruption without silently repeating completed trials.
No candidate is promoted to the production recommender by this program.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from functools import lru_cache
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import traceback
from typing import Any

from research.chronological_validation import (
    append_ledger_record, canonical, synthetic_document, verify_ledger,
)


REPO = Path(__file__).resolve().parents[1]
DEFAULT_PLAN = REPO / "research/contribution_experiment_plan.json"


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_once(path: Path, value: Any) -> None:
    with path.open("xb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def write_atomic_once(path: Path, payload: bytes) -> None:
    """Publish a complete file exclusively; verify an existing orphan exactly."""
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError("Existing durable worker output differs")
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pending-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ValueError("Concurrent durable worker output differs")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def source_fingerprint() -> dict[str, str]:
    paths = list((REPO / "custom_components/investment").rglob("*.py"))
    paths += list((REPO / "research").glob("*.py"))
    paths += list((REPO / "research/frozen_v13").glob("*.py"))
    paths += list((REPO / "tests").glob("test_contribution*.py"))
    return {str(path.relative_to(REPO)): digest(path.read_bytes()) for path in sorted(paths)}


@lru_cache(maxsize=4)
def _load_document(path: str, expected_sha256: str) -> dict[str, Any]:
    raw = Path(path).read_bytes()
    if digest(raw) != expected_sha256:
        raise ValueError("Observed source changed from its declared content identity")
    return json.loads(raw)


def document_for_scenario(plan: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    if scenario["dataset"] == "synthetic":
        return synthetic_document(scenario["regime"])
    if scenario["dataset"] != "observed":
        raise ValueError("Undeclared dataset kind")
    document = deepcopy(_load_document(str(REPO / plan["dataset"]), plan["dataset_sha256"]))
    categories = scenario.get("asset_categories")
    if categories is not None:
        document["assets"] = {symbol: row for symbol, row in document["assets"].items()
                              if row["category"] in categories}
        document["study_subset_categories"] = list(categories)
    if not document["assets"]:
        raise ValueError("Empty predeclared universe")
    return document


def experiment(parameters: dict[str, Any]):
    from research.contribution_validation import Experiment
    values = dict(parameters)
    values["initial_positions"] = tuple(tuple(row) for row in values.get("initial_positions", ()))
    values["whole_unit_categories"] = tuple(values.get("whole_unit_categories", ()))
    return Experiment(**values)


def build_trials(plan: dict[str, Any]) -> list[dict[str, Any]]:
    trials = []
    if len(plan["strategies"]) != len(set(plan["strategies"])):
        raise ValueError("Duplicate strategy in predeclared plan")
    scenario_ids = [row["id"] for row in plan["scenarios"]]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise ValueError("Duplicate scenario in predeclared plan")
    for scenario in plan["scenarios"]:
        document = document_for_scenario(plan, scenario)
        data_hash = digest(canonical(document))
        for strategy in plan["strategies"]:
            values = {**plan["defaults"], **scenario["parameters"], "strategy": strategy}
            config = experiment(values)
            for symbol, _ in config.initial_positions:
                if symbol not in document["assets"]:
                    raise ValueError("Initial position lies outside declared universe")
            trials.append({
                "trial_id": f"{len(trials)+1:03d}-{scenario['id']}-{strategy}",
                "scenario": scenario["id"], "strategy": strategy,
                "parameters": asdict(config), "dataset_sha256": data_hash,
            })
    return trials


def freeze(plan_path: Path, run_dir: Path) -> dict[str, Any]:
    plan = json.loads(plan_path.read_bytes())
    dataset_path = REPO / plan["dataset"]
    if digest(dataset_path.read_bytes()) != plan["dataset_sha256"]:
        raise ValueError("Dataset differs from the explicitly predeclared snapshot")
    trials = build_trials(plan)
    files = source_fingerprint()
    raw_paths = [REPO / plan["dataset"]]
    source = _load_document(str(dataset_path), plan["dataset_sha256"])
    for row in source["assets"].values():
        raw_path = dataset_path.parent / row["source"]["raw_snapshot"]
        raw = gzip.decompress(raw_path.read_bytes())
        if digest(raw) != row["source"]["raw_sha256"]:
            raise ValueError("Raw provider payload hash differs")
        raw_paths.append(raw_path)
    scenario_documents = {
        scenario["id"]: canonical(document_for_scenario(plan, scenario)) for scenario in plan["scenarios"]
    }
    scenario_datasets = {
        name: {"file": f"datasets/{digest(raw)}.json.gz", "sha256": digest(raw)}
        for name, raw in scenario_documents.items()
    }
    manifest = {
        "schema": "ha-investment-contribution-source-freeze-v1",
        "study_id": plan["study_id"], "baseline_commit": plan["baseline_commit"],
        "git_parent_at_freeze": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "plan_sha256": digest(canonical(plan)), "trials_sha256": digest(canonical(trials)),
        "files": files,
        "datasets": {str(path.relative_to(REPO)): digest(path.read_bytes()) for path in sorted(raw_paths)},
        "scenario_datasets": scenario_datasets,
        "interpretation": "Predeclared known-history study; not an untouched holdout or prospective performance claim",
    }
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "datasets").mkdir()
    (run_dir / "attempts").mkdir()
    for name, raw in scenario_documents.items():
        path = run_dir / scenario_datasets[name]["file"]
        if not path.exists():
            with path.open("xb") as stream:
                stream.write(gzip.compress(raw, mtime=0))
    write_once(run_dir / "predeclared_plan.json", plan)
    write_once(run_dir / "predeclared_trials.json", trials)
    write_once(run_dir / "source_manifest.json", manifest)
    with (run_dir / "experiment_ledger.jsonl").open("x", encoding="utf-8"):
        pass
    return {"study_id": plan["study_id"], "trials": len(trials),
            "source_manifest_sha256": digest((run_dir / "source_manifest.json").read_bytes())}


def verify_freeze(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    plan = json.loads((run_dir / "predeclared_plan.json").read_bytes())
    trials = json.loads((run_dir / "predeclared_trials.json").read_bytes())
    manifest = json.loads((run_dir / "source_manifest.json").read_bytes())
    if digest(canonical(plan)) != manifest["plan_sha256"]:
        raise ValueError("Predeclared plan changed")
    if digest(canonical(trials)) != manifest["trials_sha256"]:
        raise ValueError("Predeclared trials changed")
    if source_fingerprint() != manifest["files"]:
        raise ValueError("Frozen code changed; retain this attempt and create a new explicitly superseding study")
    for relative, expected in manifest["datasets"].items():
        if digest((REPO / relative).read_bytes()) != expected:
            raise ValueError(f"Frozen data changed: {relative}")
    for details in manifest["scenario_datasets"].values():
        if digest(gzip.decompress((run_dir / details["file"]).read_bytes())) != details["sha256"]:
            raise ValueError("Frozen scenario data changed")
    return plan, trials, manifest


def _durable_worker_result(directory: Path, case: dict[str, Any]):
    path = directory / "attempts" / (case["trial_id"] + ".result.json")
    if not path.exists():
        return None
    envelope = json.loads(path.read_bytes())
    record = envelope["record"]
    if digest(canonical(record)) != envelope["record_sha256"]:
        raise ValueError("Durable worker result checksum changed")
    if any(canonical(record[key]) != canonical(value) for key, value in case.items()):
        raise ValueError("Durable worker result differs from the frozen trial")
    if record["status"] == "failed":
        return record, None
    if record["status"] != "completed" or record["trace_file"] != case["trial_id"] + ".json.gz":
        raise ValueError("Invalid durable worker result")
    compressed = (directory / record["trace_file"]).read_bytes()
    raw = gzip.decompress(compressed)
    if digest(raw) != record["trace_sha256"]:
        raise ValueError("Durable worker trace checksum changed")
    trace = json.loads(raw)
    concise = {key: trace[key] for key in
               ("metrics", "development", "evaluation", "ending_positions", "execution_contract")}
    if canonical(trace["parameters"]) != canonical(case["parameters"]) or canonical(concise) != canonical(record["result"]):
        raise ValueError("Durable worker summary differs from its trace")
    return record, compressed


@lru_cache(maxsize=2)
def _frozen_panel(path: str, expected_sha256: str):
    from research.contribution_validation import PricePanel
    raw = gzip.decompress(Path(path).read_bytes())
    if digest(raw) != expected_sha256:
        raise ValueError("Trial dataset differs from its frozen identity")
    return PricePanel(json.loads(raw))


def _trial_worker(plan: dict[str, Any], case: dict[str, Any], run_dir: str) -> tuple[dict[str, Any], bytes | None]:
    from research.contribution_validation import replay
    directory = Path(run_dir)
    attempts = directory / "attempts"
    attempts.mkdir(exist_ok=True)
    retained = _durable_worker_result(directory, case)
    if retained is not None:
        return retained
    previous = sorted(attempts.glob(case["trial_id"] + ".*.started.json"))
    attempt_number = len(previous) + 1
    write_once(attempts / f"{case['trial_id']}.{attempt_number:03d}.started.json", {
        "trial": case, "attempt_number": attempt_number,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "reason": "initial_attempt" if not previous else "resume_unfinished_attempt_with_unchanged_parameters",
    })
    try:
        panel = _frozen_panel(
            str(directory / "datasets" / (case["dataset_sha256"] + ".json.gz")), case["dataset_sha256"]
        )
        result = replay(panel, experiment(case["parameters"]), capture_trace=True)
        raw = canonical(result)
        concise = {key: result[key] for key in
                   ("metrics", "development", "evaluation", "ending_positions", "execution_contract")}
        record = {**case, "status": "completed", "trace_file": case["trial_id"] + ".json.gz",
                  "trace_sha256": digest(raw), "result": concise}
        compressed = gzip.compress(raw, mtime=0)
    except Exception as err:
        record = {**case, "status": "failed", "error": f"{type(err).__name__}: {err}",
                  "error_traceback": traceback.format_exc()}
        compressed = None
    # Workers can finish out of order. Publish the complete outcome before
    # returning it to the parent, so a slow earlier trial cannot hide later
    # completed/failed attempts after interruption.
    if compressed is not None:
        write_atomic_once(directory / record["trace_file"], compressed)
    write_atomic_once(attempts / (case["trial_id"] + ".result.json"), canonical({
        "record": record, "record_sha256": digest(canonical(record)),
        "attempt_number": attempt_number,
    }) + b"\n")
    return record, compressed


def _read_ledger(run_dir: Path, trials: list[dict[str, Any]]):
    path = run_dir / "experiment_ledger.jsonl"
    count, endpoint = verify_ledger(path)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    if count > len(trials):
        raise ValueError("Ledger contains undeclared trials")
    for index, record in enumerate(records):
        if any(canonical(record[key]) != canonical(value) for key, value in trials[index].items()):
            raise ValueError("Ledger trial differs from declared order or parameters")
        if record["status"] == "completed":
            raw = gzip.decompress((run_dir / record["trace_file"]).read_bytes())
            if digest(raw) != record["trace_sha256"]:
                raise ValueError("Retained trace changed")
    return records, endpoint


def run(run_dir: Path, workers: int = 2) -> dict[str, Any]:
    if not 1 <= workers <= 8:
        raise ValueError("Workers must be between 1 and 8")
    plan, trials, manifest = verify_freeze(run_dir)
    records, endpoint = _read_ledger(run_dir, trials)
    if (run_dir / "results.json").exists():
        retained = json.loads((run_dir / "results.json").read_bytes())
        if len(records) != len(trials) or retained["ledger_last_sha256"] != endpoint:
            raise ValueError("Completed results disagree with durable ledger")
        expected_cases = [{key: value for key, value in row.items()
                           if key not in {"record_sha256", "previous_record_sha256"}} for row in records]
        if canonical(retained["cases"]) != canonical(expected_cases):
            raise ValueError("Completed case summaries disagree with durable ledger")
        completed_count = sum(row["status"] == "completed" for row in expected_cases)
        failed_count = len(expected_cases) - completed_count
        if (retained["trial_count"] != len(trials) or retained["completed_count"] != completed_count
                or retained["failed_count"] != failed_count):
            raise ValueError("Completed study counts disagree with durable ledger")
        return {"status": "already_completed", "trials": len(records),
                "completed_count": completed_count, "failed_count": failed_count}
    pending = trials[len(records):]
    # map returns in the fixed declared order, independent of worker completion
    # timing. Only pure trial evaluation is parallel; ledger writes are serial.
    with ProcessPoolExecutor(max_workers=workers) as executor:
        for record, compressed in executor.map(
            _trial_worker, [plan] * len(pending), pending, [str(run_dir)] * len(pending), chunksize=1
        ):
            if compressed is not None:
                path = run_dir / record["trace_file"]
                if path.exists():
                    # An interrupted write may have produced an orphan trace.
                    # Reuse only byte-identical deterministic output.
                    if path.read_bytes() != compressed:
                        raise ValueError("Orphan trace differs from deterministic replay")
                else:
                    with path.open("xb") as stream:
                        stream.write(compressed)
                        stream.flush()
                        os.fsync(stream.fileno())
            endpoint = append_ledger_record(run_dir / "experiment_ledger.jsonl", record, endpoint)
            records.append(record)
            print(f"{len(records)}/{len(trials)} {record['trial_id']} {record['status']}", flush=True)
    verify_freeze(run_dir)
    cases = [{key: value for key, value in record.items()
              if key not in {"record_sha256", "previous_record_sha256"}} for record in records]
    result = {
        "schema": "ha-investment-contribution-study-results-v1", "study_id": plan["study_id"],
        "git_commit_at_run": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "source_manifest_sha256": digest((run_dir / "source_manifest.json").read_bytes()),
        "source_freeze_valid": True, "ledger_last_sha256": endpoint,
        "trial_count": len(trials), "completed_count": sum(row["status"] == "completed" for row in cases),
        "failed_count": sum(row["status"] != "completed" for row in cases), "cases": cases,
        "promotion_decision": "No predictive production promotion from known history alone",
        "limitations": plan["scientific_contract"],
    }
    write_once(run_dir / "results.json", result)
    return {key: result[key] for key in ("study_id", "trial_count", "completed_count", "failed_count")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freezer = sub.add_parser("freeze")
    freezer.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    freezer.add_argument("--run-dir", type=Path, required=True)
    runner = sub.add_parser("run")
    runner.add_argument("--run-dir", type=Path, required=True)
    runner.add_argument("--workers", type=int, default=2)
    verifier = sub.add_parser("verify")
    verifier.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        result = freeze(args.plan.resolve(), args.run_dir.resolve())
    elif args.command == "run":
        result = run(args.run_dir.resolve(), args.workers)
    else:
        plan, trials, _ = verify_freeze(args.run_dir.resolve())
        records, endpoint = _read_ledger(args.run_dir.resolve(), trials)
        result = {"study_id": plan["study_id"], "source_freeze_valid": True,
                  "retained_trials": len(records), "expected_trials": len(trials), "ledger_last_sha256": endpoint}
    print(json.dumps(result, sort_keys=True, allow_nan=False), flush=True)
    if result.get("failed_count", 0):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
