from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "investment"


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_holding_provider_is_stored_on_buy_lots_not_holdings():
    storage = text("storage.py")
    assert '"holding_provider_id": normalized_holding_provider_id or ""' in storage
    assert 'existing["holding_provider_id"] = normalized_holding_provider_id' not in storage
    assert '"average_buy_price": average_buy_price,\n                "holding_provider_id"' not in storage


def test_portfolio_exposes_fifo_remaining_units_by_provider():
    ledger = text("ledger.py")
    manager = text("manager.py")
    assert "def holding_provider_balances(" in ledger
    assert 'row.get("remaining_quantity")' in ledger
    assert "holding_provider_balances," in manager
    assert '"holding_provider_balances": holding_provider_balances(ledger.rows, provider_names)' in manager


def test_frontend_assigns_provider_per_buy_and_shows_lot_provider():
    panel = text("www/investment-panel.js")
    assert 'holding_provider_id:String(row.holding_provider_id||"")' in panel
    assert 'holding_provider_id:"",transaction_date:today' in panel
    assert 'class="ledger-provider"' in panel
    assert "holding_provider_balances" in panel
    assert "data-holding-provider-select" not in panel
    assert "data-expand=" not in panel


def test_trend_preview_values_are_pointer_near_and_interactive():
    panel = text("www/investment-panel.js")
    assert ".trend-pop.preview{pointer-events:auto}" in panel
    assert "data-trend-tooltip-change" in panel
    assert "showTrendPoint(chart,globalIndex,clientX=null,clientY=null)" in panel
    assert "loadTrend(scope,id,anchor,period,{x:intent.x,y:intent.y},intent.metric)" in panel
    assert "},260);" in panel
    assert 'if(this._trendPinned||e.target.closest("button"))return;' in panel
