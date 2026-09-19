from pathlib import Path

PANEL = (Path(__file__).resolve().parents[1] / "custom_components" / "investment" / "www" / "investment-panel.js").read_text(encoding="utf-8")


def test_allocation_explorer_supports_category_provider_and_currency_modes():
    assert 'this._allocationMode="category"' in PANEL
    assert "allocationRows(portfolio=this._portfolio,mode=this._allocationMode)" in PANEL
    assert 'data-allocation-mode="provider"' in PANEL
    assert 'data-allocation-mode="currency"' in PANEL


def test_provider_allocation_uses_remaining_fifo_provider_balances():
    assert "holding_provider_balances" in PANEL
    assert "units/quantity" in PANEL
    assert 'this.t("unassignedProvider")' in PANEL


def test_currency_allocation_groups_current_values_by_quote_currency():
    assert "holding?.quote_currency||holding?.currency" in PANEL
    assert "current.value+=value" in PANEL


def test_category_allocation_keeps_navigation_to_category_cards():
    assert "data-allocation-category" in PANEL
    assert "scrollIntoView" in PANEL
