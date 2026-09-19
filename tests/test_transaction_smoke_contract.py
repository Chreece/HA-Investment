from pathlib import Path
import importlib.util
import math
import sys

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components" / "investment"
PANEL = (COMP / "www" / "investment-panel.js").read_text(encoding="utf-8")


def load_ledger_module():
    spec = importlib.util.spec_from_file_location("investment_ledger_smoke", COMP / "ledger.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_two_provider_buy_lots_partial_fifo_sell_keep_provider_balances_correct():
    ledger = load_ledger_module()
    records = [
        {
            "id": "buy_x",
            "type": "buy",
            "sort_ts": 1_725_000_100,
            "quantity": 2.0,
            "cash_principal": 200.0,
            "explicit_costs": 4.0,
            "holding_provider_id": "provider_x",
        },
        {
            "id": "buy_y",
            "type": "buy",
            "sort_ts": 1_725_864_200,
            "quantity": 3.0,
            "cash_principal": 360.0,
            "explicit_costs": 6.0,
            "holding_provider_id": "provider_y",
        },
        {
            "id": "sell_1",
            "type": "sell",
            "sort_ts": 1_726_900_300,
            "quantity": 2.5,
            "net_proceeds": 350.0,
            "explicit_costs": 2.0,
        },
    ]

    result = ledger.fifo_summary(records, current_price=150.0)
    rows = {row["id"]: row for row in result.rows}

    # FIFO closes provider X's 2-unit lot and consumes 0.5 unit from provider Y.
    assert math.isclose(result.quantity, 2.5)
    assert rows["buy_x"]["status"] == "closed"
    assert math.isclose(rows["buy_x"]["remaining_quantity"], 0.0)
    assert rows["buy_y"]["status"] == "partial"
    assert math.isclose(rows["buy_y"]["remaining_quantity"], 2.5)
    assert rows["sell_1"]["fifo_allocations"] == [
        {"buy_id": "buy_x", "quantity": 2.0, "cost_basis": 204.0},
        {"buy_id": "buy_y", "quantity": 0.5, "cost_basis": 61.0},
    ]

    # Remaining basis is 2.5 * 122 = 305; realized result is 350 - 265 = 85.
    assert math.isclose(result.remaining_cost_basis, 305.0)
    assert math.isclose(result.realized_pnl, 85.0)

    balances = ledger.holding_provider_balances(
        result.rows,
        {"provider_x": "Broker X", "provider_y": "Broker Y"},
    )
    assert balances == [
        {"id": "provider_y", "name": "Broker Y", "quantity": 2.5},
    ]


def test_smoke_contract_keeps_exact_ledger_time_and_card_privacy_wiring():
    # Exact transaction history is keyed by ledger row sort_ts and grouped at that
    # timestamp, rather than projected onto the next market-history sample.
    assert "trendScopeLedgerEvents(tr=this._trend)" in PANEL
    assert "sort_ts:ts" in PANEL
    assert "ledgerMetricSeries(events=[],startTs=0,endTs=Math.floor(Date.now()/1000))" in PANEL
    assert "while(index<sorted.length&&Number(sorted[index].sort_ts)===ts)" in PANEL
    assert "push(ts);" in PANEL

    # Category/holding cards reuse those local ledger series and stay privacy-safe.
    assert 'cardLedgerSeries(scope,id,period="1m")' in PANEL
    assert 'const ledgerSeries=this.cardLedgerSeries("holding",h.id);' in PANEL
    assert 'metricMiniChartHtml(ledgerSeries.costBasis,"costBasis","accent")' in PANEL
    assert '.incognito .kpi-spark:not([data-incognito-revealed])' in PANEL
    assert 'data-history-metric="costBasis"' in PANEL
    assert 'data-history-metric="costs"' in PANEL
    assert 'data-history-metric="assetFees"' in PANEL
