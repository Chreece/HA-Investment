"""Resume only an interrupted experiment's unledgered trials after strict checks.

The original engine, economic plan, data and model content hashes must match.
Completed records are never overwritten. Any orphan trace from an interrupted
append must match a deterministic recomputation byte-for-byte before reuse.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import platform

import chronological_validation as engine


def resume(directory: Path, data_path: Path):
    if (directory / "results.json").exists():
        raise ValueError("Run already has a final result; recovery cannot replace it")
    meta = json.loads((directory / "provenance.json").read_text())
    fingerprint = engine.source_fingerprint()
    if fingerprint != meta["source_fingerprint"]:
        raise ValueError("Original engine/model sources changed; cannot resume")
    data_bytes = data_path.read_bytes()
    if engine.sha256(data_bytes) != meta["data_sha256"]:
        raise ValueError("Original source snapshot changed; cannot resume")
    plan_bytes = (directory / "predeclared_plan.json").read_bytes()
    if engine.sha256(plan_bytes) != meta["plan_sha256"]:
        raise ValueError("Original economic plan changed; cannot resume")
    dataset, plan = json.loads(data_bytes), json.loads(plan_bytes)
    suite = list(engine.experiment_suite(plan, dataset))
    ledger_path = directory / "experiment_ledger.jsonl"
    completed, last_digest = engine.verify_ledger(ledger_path)
    records = []
    for index, line in enumerate(ledger_path.read_text().splitlines(), 1):
        row = json.loads(line)
        row.pop("record_sha256")
        row.pop("previous_record_sha256")
        label, document, config = suite[index - 1]
        expected_id = f"{index:03d}-{label}-{config.risk}-{config.strategy}"
        if row["trial_id"] != expected_id or engine.canonical(row["parameters"]) != engine.canonical(asdict(config)):
            raise ValueError("Retained trial does not match the predeclared sequence")
        if row["data_sha256"] != engine.sha256(engine.canonical(document)):
            raise ValueError("Retained trial data changed")
        if row["code_fingerprint_sha256"] != engine.sha256(engine.canonical(fingerprint)):
            raise ValueError("Retained trial used a different engine/model source")
        if row["status"] == "completed":
            raw = gzip.decompress((directory / row["trace_file"]).read_bytes())
            if engine.sha256(raw) != row["trace_sha256"]:
                raise ValueError("Retained trace content changed")
        records.append(row)
    if len(records) != completed or completed > len(suite):
        raise ValueError("Retained ledger length is inconsistent")
    checkpoint = {
        "status": "resuming_unledgered_trials_only", "completed_prefix_verified": completed,
        "planned_trials": len(suite), "source_data_and_plan_unchanged": True,
        "retained_ledger_endpoint": last_digest, "resumed_at_utc": datetime.now(timezone.utc).isoformat(),
        "resume_runner_sha256": engine.sha256(Path(__file__).read_bytes()), "python_version": platform.python_version(),
        "reason": "Original tool/process returned a failure before final completion; no Python traceback established a cause. Valid completed trial records and orphan traces were retained.",
    }
    recovery_number = len(list(directory.glob("recovery-*.json"))) + 1
    recovery_path = directory / f"recovery-{recovery_number:02d}.json"
    with recovery_path.open("x") as stream:
        stream.write(engine.canonical(checkpoint).decode() + "\n")
    print(f"Verified {completed}/{len(suite)} retained trials; resuming unchanged plan", flush=True)
    reused_orphans = []
    for index in range(completed + 1, len(suite) + 1):
        label, document, config = suite[index - 1]
        trial_id = f"{index:03d}-{label}-{config.risk}-{config.strategy}"
        record = {"trial_id": trial_id, "label": label, "parameters": asdict(config),
                  "data_sha256": engine.sha256(engine.canonical(document)),
                  "code_fingerprint_sha256": engine.sha256(engine.canonical(fingerprint))}
        try:
            result = engine.replay(document, config, capture_trace=True)
            trace_bytes = engine.canonical({"trace": result.pop("trace"), "daily_accounting": result.pop("daily_accounting")})
            trace_name = trial_id + ".json.gz"
            trace_path = directory / trace_name
            if trace_path.exists():
                if gzip.decompress(trace_path.read_bytes()) != trace_bytes:
                    raise RuntimeError("Orphan trace differs from deterministic recomputation; evidence preserved, recovery stopped")
                reused_orphans.append(trial_id)
            else:
                with trace_path.open("xb") as stream:
                    stream.write(gzip.compress(trace_bytes, mtime=0))
            record.update(status="completed", result=result, trace_file=trace_name, trace_sha256=engine.sha256(trace_bytes))
            print(f"{trial_id}: completed", flush=True)
        except Exception as error:
            # An orphan mismatch is a protocol failure: do not overwrite the
            # trace or label it as an ordinary model outcome.
            if isinstance(error, RuntimeError) and "Orphan trace" in str(error):
                raise
            record.update(status="failed", error=f"{type(error).__name__}: {error}")
            print(f"{trial_id}: FAILED {record['error']}", flush=True)
        last_digest = engine.append_ledger_record(ledger_path, record, last_digest)
        records.append(record)
    count, endpoint = engine.verify_ledger(ledger_path)
    meta.update({"completed_at_utc": datetime.now(timezone.utc).isoformat(),
                 "source_freeze_valid": engine.source_fingerprint() == fingerprint,
                 "ledger_verified_complete": count == len(records) == len(suite) and endpoint == last_digest,
                 "ledger_last_sha256": last_digest, "checkpoint_recovery": checkpoint,
                 "orphan_traces_recomputed_and_matched": reused_orphans})
    with (directory / "results.json").open("x") as stream:
        stream.write(engine.canonical({"provenance": meta, "results": records}).decode() + "\n")
    with (directory / "REPORT.md").open("x") as stream:
        stream.write(engine.render_report(records, meta))
    with (directory / "recovery-completed.json").open("x") as stream:
        stream.write(engine.canonical({"completed_prefix": completed, "new_records": len(suite) - completed,
                                       "matched_orphan_traces": reused_orphans,
                                       "ledger_complete": meta["ledger_verified_complete"]}).decode() + "\n")
    if not meta["source_freeze_valid"] or not meta["ledger_verified_complete"] or any(r["status"] != "completed" for r in records):
        raise SystemExit(1)
    print(f"All {len(records)} trials retained with valid source and ledger; independent audit still required", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    resume(args.run_dir, args.data)


if __name__ == "__main__":
    main()
