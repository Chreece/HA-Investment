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
    manager = text("manager.py")
    assert "def holding_provider_balances(" in manager
    assert 'row.get("remaining_quantity")' in manager
    assert '"holding_provider_balances": holding_provider_balances(ledger.rows)' in manager


def test_frontend_assigns_provider_per_buy_and_shows_lot_provider():
    panel = text("www/investment-panel.js")
    assert 'holding_provider_id:String(row.holding_provider_id||"")' in panel
    assert 'holding_provider_id:"",transaction_date:today' in panel
    assert 'class="ledger-provider"' in panel
    assert "holding_provider_balances" in panel
    assert "data-holding-provider-select" not in panel
