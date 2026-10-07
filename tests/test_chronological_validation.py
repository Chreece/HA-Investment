"""Independent accounting, chronology and estimator checks for the replay."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import random
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("investment_chronological_research_test", ROOT / "research" / "chronological_validation.py")
research = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = research
SPEC.loader.exec_module(research)


def panel_from_prices(prices, dates=None, inception=None):
    dates = dates or [(date(2024, 1, 1) + timedelta(days=i)).isoformat() for i in range(len(prices))]
    bars = [{"date": day, "ts": int(datetime.combine(date.fromisoformat(day), datetime.min.time(), timezone.utc).timestamp()) + 12 * 3600,
             "close": price, "adjusted_close": price} for day, price in zip(dates, prices)]
    return {"kind": "synthetic_adversarial_scenario", "assets": {"TEST": {
        "name": "Synthetic MSCI World", "sleeve": "broad_equity", "inception": inception or dates[0],
        "category": "etf", "currency": "EUR", "bars": bars}}}


def test_frozen_comparison_files_match_recorded_original_hashes():
    for filename, expected in research.BASELINE_HASHES.items():
        assert hashlib.sha256((ROOT / "research" / "frozen_v13" / filename).read_bytes()).hexdigest() == expected


@pytest.mark.parametrize("filename", ["instrument_identity.py", "purchase_search.py"])
def test_source_fingerprint_covers_live_dynamic_and_purchase_dependencies(filename, monkeypatch):
    target = ROOT / "custom_components" / "investment" / filename
    key = str(target.relative_to(ROOT))
    before = research.source_fingerprint()
    assert key in before
    original = Path.read_bytes

    def altered_bytes(path):
        content = original(path)
        return content + b"\n# independent fingerprint probe\n" if path == target else content

    monkeypatch.setattr(Path, "read_bytes", altered_bytes)
    after = research.source_fingerprint()
    assert before[key] != after[key]


def test_plan_is_explicit_about_all_trials_and_no_automatic_promotion():
    plan = json.loads((ROOT / "research" / "experiment_plan.json").read_text())
    assert plan["declared_before_outcomes"] is True
    assert len(list(research.experiment_suite(plan, panel_from_prices([1, 2])))) == 84
    assert plan["baseline_commit"] == research.BASELINE_COMMIT


def test_es_integrates_fractional_tail_against_hand_calculation():
    observations = [-.20, -.05, -.01] + [.01] * 49
    assert research.exact_es(observations) == pytest.approx((.20 + .05 + .6 * .01) / 2.6)
    assert research.exact_es([-.40] + [0] * 99) == pytest.approx(.08)


@pytest.mark.parametrize("n", [30, 52, 60, 99, 100, 159, 300])
def test_es_matches_independent_loss_quantile_grid(n):
    rng = random.Random(n)
    returns = [rng.uniform(-.4, .25) for _ in range(n)]
    # Each observation carries 100 equal mass pieces; 5% of the resulting
    # 100*n pieces is an integer. This is an independent exact discrete oracle.
    pieces = sorted([-r for r in returns for _ in range(100)], reverse=True)
    count = n * 5
    expected = max(0, sum(pieces[:count]) / count)
    assert research.exact_es(returns) == pytest.approx(expected)


@pytest.mark.parametrize("observations", [[True], [float("nan")], [-1.01], [float("inf")]])
def test_es_rejects_invalid_observations(observations):
    with pytest.raises(ValueError):
        research.exact_es(observations)


def test_fixed_shrinkage_has_independent_closed_form_oracle():
    matrix = [[1., 2.], [2., 4.], [3., 6.]]
    assert research.covariance(matrix, 0) == [[1., 2.], [2., 4.]]
    assert research.covariance(matrix, .5) == [[1., 1.], [1., 4.]]
    assert research.covariance(matrix, 1) == [[1., 0.], [0., 4.]]
    assert research.covariance_volatility(matrix, [.25, .75], .5) == pytest.approx(math.sqrt((.0625 + .375 + 2.25) * 52))


def test_sample_covariance_portfolio_variance_matches_direct_returns():
    matrix = [[-.04, .03, .01], [.08, -.06, 0], [.01, .02, .005], [-.02, .06, .002]]
    weights = [.2, .35, .15]
    portfolio = [sum(x * w for x, w in zip(row, weights)) for row in matrix]
    average = sum(portfolio) / len(portfolio)
    variance = sum((x - average) ** 2 for x in portfolio) / (len(portfolio) - 1)
    assert research.covariance_volatility(matrix, weights) ** 2 == pytest.approx(52 * variance)


@pytest.mark.parametrize("intensity", [0, .25, .5, .75, 1])
def test_fixed_shrinkage_preserves_nonnegative_quadratic_forms(intensity):
    rng = random.Random(1729)
    observations = [[rng.gauss(0, .1) for _ in range(4)] for _ in range(30)]
    for _ in range(20):
        weights = [rng.uniform(-1, 1) for _ in range(4)]
        assert research.covariance_volatility(observations, weights, intensity) >= 0


@pytest.mark.parametrize("matrix", [[[1, 2], [2]], [[True], [1]], [[1], [float("nan")]], [[1]]])
def test_covariance_rejects_invalid_aligned_inputs(matrix):
    with pytest.raises(ValueError):
        research.covariance(matrix)


def test_panel_filters_exact_inception_without_borrowing_old_returns():
    doc = panel_from_prices([1, 2, 3, 4], inception="2024-01-03")
    panel = research.PricePanel(doc)
    assert panel.history("TEST", "2024-01-02") == []
    assert [b["close"] for b in panel.history("TEST", "2024-01-04")] == [3, 4]


def test_panel_does_not_supply_a_future_or_missing_execution_price():
    panel = research.PricePanel(panel_from_prices([10, 500], dates=["2024-01-01", "2024-01-03"]))
    assert panel.observed("TEST", "2024-01-02") is None
    assert panel.history("TEST", "2024-01-02")[-1]["close"] == 10
    with pytest.raises(ValueError):
        panel.mark("TEST", "2024-01-20")


@pytest.mark.parametrize("prices", [[1, float("nan")], [1, 0], [1, True], [1, float("inf")]])
def test_panel_rejects_invalid_prices(prices):
    with pytest.raises(ValueError):
        research.PricePanel(panel_from_prices(prices))


def test_whole_lot_execution_matches_bruteforce_affordability():
    rng = random.Random(41)
    for _ in range(100):
        price = round(rng.uniform(.15, 150), 3)
        cash = round(rng.uniform(1, 500), 2)
        amount = round(rng.uniform(0, cash), 2)
        fixed = rng.choice([0, .25, 1, 3])
        bps = rng.choice([0, 5, 10, 25])
        costs = research.Costs(fixed_fee=fixed, execution_bps=bps)
        doc = panel_from_prices([price])
        panel = research.PricePanel(doc)
        quantities = {}
        balance, trades, _ = research.execute_currency_orders([{"symbol": "TEST", "suggested_amount": amount}], panel, "2024-01-01", quantities, cash, costs, True)
        feasible = []
        for n in range(math.floor(cash / price) + 1):
            principal = Decimal(str(price)) * n
            debit_principal = principal.quantize(Decimal(".01"), rounding="ROUND_UP")
            cost = costs.charges(debit_principal)
            if debit_principal <= Decimal(str(amount)) and debit_principal + cost <= Decimal(str(cash)):
                feasible.append(n)
        assert quantities.get("TEST", 0) == max(feasible or [0])
        assert balance >= 0
        assert balance + sum(t["cash_debit"] for t in trades) == pytest.approx(cash)


def test_execution_uses_next_observed_price_and_respects_currency_ceiling():
    panel = research.PricePanel(panel_from_prices([10, 20]))
    quantities = {}
    balance, trades, _ = research.execute_currency_orders([{"symbol": "TEST", "suggested_amount": 100}], panel, "2024-01-02", quantities, 100, research.Costs(0, 0, 0), True)
    assert quantities == {"TEST": 5}
    assert trades[0]["price"] == 20
    assert balance == 0


def test_currency_budget_protocol_can_buy_more_units_after_price_falls():
    panel = research.PricePanel(panel_from_prices([20, 10]))
    quantities = {}
    # Five units were estimated at the decision price. This research protocol
    # buys a currency amount; it explicitly is not a fixed-five-unit order.
    balance, trades, _ = research.execute_currency_orders(
        [{"symbol": "TEST", "suggested_amount": 100, "suggested_units": 5}],
        panel, "2024-01-02", quantities, 100, research.Costs(0, 0, 0), True,
    )
    assert quantities == {"TEST": 10}
    assert trades[0]["principal"] == 100
    assert balance == 0


def test_missing_execution_observation_keeps_cash_and_rejects_order():
    panel = research.PricePanel(panel_from_prices([10, 20], dates=["2024-01-01", "2024-01-03"]))
    balance, trades, rejected = research.execute_currency_orders([{"symbol": "TEST", "suggested_amount": 100}], panel, "2024-01-02", {}, 100, research.Costs(0, 0, 0), False)
    assert balance == 100
    assert trades == []
    assert rejected[0]["reason"] == "no_execution_observation"


def test_fee_reserve_prevents_overspending_fractional_purchase():
    panel = research.PricePanel(panel_from_prices([.01234]))
    quantity = {}
    balance, trades, _ = research.execute_currency_orders([{"symbol": "TEST", "suggested_amount": 100}], panel, "2024-01-01", quantity, 100, research.Costs(1, 0, 10), False)
    assert balance >= 0
    assert sum(t["cash_debit"] for t in trades) <= 100
    assert trades[0]["fees"] >= 1


def fixed_full_purchase(panel, cutoff, quantities, cash, config, state, finalizer=None):
    return [{"symbol": "TEST", "suggested_amount": cash + config.contribution}], [], None


def test_deposits_do_not_hide_twenty_percent_investment_loss(monkeypatch):
    document = panel_from_prices([100, 100, 80, 80], dates=["2024-01-30", "2024-01-31", "2024-02-01", "2024-02-02"])
    monkeypatch.setattr(research, "decide", fixed_full_purchase)
    config = research.Experiment(strategy="frozen_v13", start="2024-01-31", end="2024-02-02", fixed_fee=0, execution_bps=0)
    result = research.replay(document, config)
    assert result["metrics"]["contributions"] == 200
    assert result["metrics"]["ending_nominal_wealth"] == pytest.approx(180)
    assert result["metrics"]["max_unitized_drawdown"] == pytest.approx(.2)
    assert result["metrics"]["time_weighted_total_return_after_costs"] == pytest.approx(-.2)


def test_flat_prices_and_contributions_have_zero_return_without_costs(monkeypatch):
    document = panel_from_prices([10, 10, 10, 10], dates=["2024-01-30", "2024-01-31", "2024-02-01", "2024-02-02"])
    monkeypatch.setattr(research, "decide", fixed_full_purchase)
    result = research.replay(document, research.Experiment(strategy="frozen_v13", start="2024-01-31", end="2024-02-02", fixed_fee=0, execution_bps=0))
    assert result["metrics"]["ending_nominal_wealth"] == pytest.approx(200)
    assert result["metrics"]["time_weighted_total_return_after_costs"] == pytest.approx(0)
    assert result["metrics"]["max_unitized_drawdown"] == pytest.approx(0)


def test_float_cash_addition_cannot_silently_erase_one_cent(monkeypatch):
    assert str(102.27 + 100.0) == "202.26999999999998"
    assert research.add_currency(102.27, 100.0) == 202.27
    document = panel_from_prices([10, 10], dates=["2024-01-30", "2024-01-31"])
    monkeypatch.setattr(research, "decide", fixed_full_purchase)
    result = research.replay(document, research.Experiment(
        strategy="frozen_v13", initial_cash=102.27, start="2024-01-31", end="2024-01-31", fixed_fee=0, execution_bps=0,
    ))
    assert result["metrics"]["ending_nominal_wealth"] == pytest.approx(202.27)
    assert result["metrics"]["time_weighted_total_return_after_costs"] == pytest.approx(0)


def test_fees_reduce_nav_even_on_first_contribution(monkeypatch):
    document = panel_from_prices([10, 10], dates=["2024-01-30", "2024-01-31"])
    monkeypatch.setattr(research, "decide", fixed_full_purchase)
    result = research.replay(document, research.Experiment(strategy="frozen_v13", start="2024-01-31", end="2024-01-31", fixed_fee=1, execution_bps=0))
    assert result["metrics"]["ending_nominal_wealth"] == pytest.approx(99)
    assert result["metrics"]["time_weighted_total_return_after_costs"] == pytest.approx(-.01)
    assert result["metrics"]["total_execution_costs"] == pytest.approx(1)


def test_later_data_cannot_change_earlier_decision_inputs():
    document = research.synthetic_document()
    changed = json.loads(json.dumps(document))
    cutoff = "2022-01-03"
    for asset in changed["assets"].values():
        for row in asset["bars"]:
            if row["date"] > cutoff:
                row["close"] *= 100
                row["adjusted_close"] *= 100
    model, scorer = research.load_stack(True)
    original_rows = research.prepared_candidates(research.PricePanel(document), cutoff, {}, 0, model, scorer)
    altered_rows = research.prepared_candidates(research.PricePanel(changed), cutoff, {}, 0, model, scorer)
    assert original_rows == altered_rows


def test_evaluation_partition_uses_boundary_nav_instead_of_restarting_positions():
    points = [{"date": "2025-01-02", "nav": .88, "wealth": 100, "cash": 20},
              {"date": "2025-01-03", "nav": .99, "wealth": 112.5, "cash": 20}]
    result = research.unitized_metrics(points, beginning_nav=.80)
    assert result["time_weighted_total_return_after_costs"] == pytest.approx(.99 / .8 - 1)
    assert result["max_unitized_drawdown"] == 0


def test_ledger_is_write_once_hash_chained_and_keeps_failed_trials(tmp_path):
    records = [{"trial_id": "a", "status": "failed", "error": "deliberate"}, {"trial_id": "b", "status": "completed"}]
    path = tmp_path / "ledger.jsonl"
    last = research.write_ledger(path, records)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["status"] == "failed"
    assert rows[0]["previous_record_sha256"] is None
    assert rows[1]["previous_record_sha256"] == rows[0]["record_sha256"]
    for row in rows:
        digest = row.pop("record_sha256")
        assert hashlib.sha256(research.canonical(row)).hexdigest() == digest
    assert last == json.loads(path.read_text().splitlines()[-1])["record_sha256"]
    with pytest.raises(FileExistsError):
        research.write_ledger(path, records)


def test_synced_ledger_refuses_missing_middle_record(tmp_path):
    path = tmp_path / "ledger.jsonl"
    research.write_ledger(path, [])
    first = research.append_ledger_record(path, {"trial_id": "a"}, None)
    second = research.append_ledger_record(path, {"trial_id": "b"}, first)
    third = research.append_ledger_record(path, {"trial_id": "c"}, second)
    assert research.verify_ledger(path) == (3, third)
    lines = path.read_text().splitlines()
    path.write_text(lines[0] + "\n" + lines[2] + "\n")
    with pytest.raises(ValueError, match="hash chain"):
        research.verify_ledger(path)
    with pytest.raises(ValueError, match="hash chain"):
        research.append_ledger_record(path, {"trial_id": "d"}, third)


def test_real_data_snapshot_validates_local_source_hashes():
    import gzip
    document = json.loads((ROOT / "research" / "data" / "yahoo-xetra-20261006.json").read_text())
    panel = research.PricePanel(document)
    assert len(panel.assets) == 5
    for asset in panel.assets.values():
        source = asset["source"]
        raw = gzip.decompress((ROOT / "research" / "data" / source["raw_snapshot"]).read_bytes())
        assert hashlib.sha256(raw).hexdigest() == source["raw_sha256"]
        assert all(bar["date"] < source["incomplete_session_excluded"] for bar in asset["bars"])
