from pathlib import Path

PANEL = (Path(__file__).resolve().parents[1] / "custom_components" / "investment" / "www" / "investment-panel.js").read_text(encoding="utf-8")


def test_dashboard_loads_real_portfolio_history_for_kpi_sparklines():
    assert 'async loadDashboardHistory(force=false)' in PANEL
    assert 'type:"investment/history",scope:"portfolio",period:"1m"' in PANEL
    assert "dashboardSeries(portfolio=this._portfolio)" in PANEL
    assert "miniSparklineHtml(points,tone" in PANEL


def test_dashboard_contains_allocation_donut_and_monthly_flows():
    assert "allocationDonutHtml(portfolio=this._portfolio)" in PANEL
    assert 'class="allocation-donut"' in PANEL
    assert "monthlyFlowsHtml(portfolio=this._portfolio)" in PANEL
    assert 'class="flow-chart"' in PANEL
    assert 'data-allocation-category' in PANEL


def test_dashboard_visuals_respect_incognito_mode():
    assert '".allocation-card", ".flow-card"' in PANEL
    assert "allocation-card:not([data-incognito-revealed])" in PANEL
    assert "flow-card:not([data-incognito-revealed])" in PANEL


def test_kpi_cards_keep_all_existing_portfolio_metrics_visible():
    for token in ("today_change", "asset_principal", "other_cost_total", "asset_fee_value", "all_in_cost", "pnl"):
        assert token in PANEL
