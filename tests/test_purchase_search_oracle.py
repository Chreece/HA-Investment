"""Exhaustive independent arithmetic oracle for bounded whole-unit repairs."""
from copy import deepcopy
from decimal import Decimal
import importlib.util
import itertools
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("_independent_purchase_search_audit_test", ROOT / "research/audit_purchase_search.py")
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


@pytest.fixture(scope="module")
def subject():
    return audit.load_subject(name="_independent_purchase_search_subject")


def validate_optimum(subject, specification):
    original = deepcopy(specification)
    rows, meta = audit.call_subject(subject, specification)
    check, violations = audit.check_output(specification, rows, meta)
    oracle = audit.exhaustive_oracle(specification)
    assert not violations, (specification["name"], violations)
    assert (check["principal"], check["cost"]) == (oracle["best_principal"], oracle["best_cost"]), (
        specification["name"], [row["suggested_units"] for row in rows], oracle)
    assert specification == original
    json.dumps(meta, allow_nan=False)
    return rows, meta, oracle


def test_oracle_counterexample_has_independent_closed_form_solution():
    specification = next(audit.predefined_cases())
    oracle = audit.exhaustive_oracle(specification)
    assert oracle["domain_size"] == 8
    assert oracle["best_principal"] == Decimal(70)
    assert oracle["best_cost"] == Decimal(2)
    assert set(oracle["optimal_vectors"]) == {(Decimal(1), Decimal(1), Decimal(0)),
                                             (Decimal(1), Decimal(0), Decimal(1))}


@pytest.mark.parametrize("specification", list(audit.predefined_cases()), ids=lambda specification: specification["name"])
def test_predefined_integer_domains_match_independent_optimum(subject, specification):
    validate_optimum(subject, specification)


@pytest.mark.parametrize("specification", list(audit.generated_cases()), ids=lambda specification: specification["name"])
def test_generated_small_integer_domains_match_independent_optimum(subject, specification):
    validate_optimum(subject, specification)


def test_integer_repair_is_deterministic_under_unique_identity_reordering(subject):
    specification = next(audit.predefined_cases())
    expected = None
    for permuted in itertools.permutations(specification["rows"]):
        candidate = {**specification, "rows": list(permuted)}
        rows, meta, _ = validate_optimum(subject, candidate)
        units = {row["provider_id"]: row["suggested_units"] for row in rows}
        if expected is None:
            expected = units
        assert units == expected
        assert meta["purchase_search"]["optimality_proven"] is True


def test_reapplication_never_restores_removed_whole_units(subject):
    specification = next(audit.predefined_cases())
    rows, first_meta, _ = validate_optimum(subject, specification)
    reduced = deepcopy(specification)
    reduced["rows"] = rows
    output, metadata = audit.call_subject(subject, reduced)
    assert [row["suggested_units"] for row in output] == [row["suggested_units"] for row in rows]
    assert metadata["execution_costs"]["principal"] == first_meta["execution_costs"]["principal"]


def test_fractional_quantities_stay_at_verified_proportional_incumbent(subject):
    specification = next(audit.predefined_cases())
    specification["rows"].append(audit.make_row("FRACTIONAL", 10., 1., sleeve="broad_equity", whole_units_only=False))
    with audit.frozen_baseline() as (baseline, _):
        incumbent, _ = audit.call_subject(baseline, specification)
    fixed = {3: incumbent[3]["suggested_units"]}
    oracle = audit.exhaustive_oracle(specification, fixed_fractional=fixed)
    rows, metadata = audit.call_subject(subject, specification)
    check, violations = audit.check_output(specification, rows, metadata)
    assert not violations
    assert rows[3]["suggested_units"] == incumbent[3]["suggested_units"]
    assert (check["principal"], check["cost"]) == (oracle["best_principal"], oracle["best_cost"])
    assert metadata["purchase_search"]["fractional_incumbent_fixed"] is True


def test_bounded_search_never_claims_a_small_domain_was_exhausted(subject, monkeypatch):
    search_module = sys.modules[subject.__package__ + ".purchase_search"]
    monkeypatch.setattr(search_module, "MAX_WHOLE_SEARCH_STATES", 4)
    specification = audit.case("forced_bounded", [
        audit.make_row("A", 40., 3, sleeve="cash_like"),
        audit.make_row("B", 30., 3), audit.make_row("C", 30., 3, sleeve="aggregate_bond")], cash=100.)
    rows, metadata = audit.call_subject(subject, specification)
    _, violations = audit.check_output(specification, rows, metadata)
    assert not violations
    search = metadata["purchase_search"]
    assert search["status"] == "bounded"
    assert search["optimality_proven"] is False
    assert search["full_domain_enumerated"] is False
    assert search["global_optimality_claimed"] is False
    assert search["generated"] <= 4
    assert search["examined"] <= search["risk_evaluation_limit"]
    assert search["no_positive_feasible"] is (False if metadata["deployed"] > 0 else None)


def test_risk_work_budget_reports_incomplete_search_without_weakening_constraints(subject, monkeypatch):
    search_module = sys.modules[subject.__package__ + ".purchase_search"]
    monkeypatch.setattr(search_module, "MAX_SEARCH_RETURN_POINTS", 1)
    specification = audit.case("risk_work_bound", [
        audit.make_row("A", 30., 3, sleeve="broad_equity", values=[.20, -.20] * 26),
        audit.make_row("B", 30., 3, values=[-.20, .20] * 26),
        audit.make_row("C", 30., 3, sleeve="aggregate_bond", values=[.20, -.20] * 26)],
        cap=.45)
    rows, metadata = audit.call_subject(subject, specification)
    _, violations = audit.check_output(specification, rows, metadata)
    assert not violations
    search = metadata["purchase_search"]
    assert search["risk_evaluation_limit"] == 1
    assert search["examined"] <= 1
    if search["stopped_by_work_limit"]:
        assert search["status"] == "bounded"
        assert search["optimality_proven"] is False
        assert search["no_positive_feasible"] is (False if metadata["deployed"] > 0 else None)


def test_audit_refuses_existing_output_directory(tmp_path):
    with pytest.raises(FileExistsError):
        audit.run_audit(tmp_path)
