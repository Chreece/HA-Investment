from pathlib import Path

PANEL = (Path(__file__).resolve().parents[1] / "custom_components" / "investment" / "www" / "investment-panel.js").read_text(encoding="utf-8")


def test_dashboard_loads_real_portfolio_history_for_kpi_sparklines():
    assert 'async loadDashboardHistory(force=false)' in PANEL
    assert 'type:"investment/history",scope:"portfolio",period:"1m"' in PANEL
    assert "dashboardSeries(portfolio=this._portfolio)" in PANEL
    assert 'metricMiniChartHtml(points,metric="value",tone="accent")' in PANEL


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


def test_dashboard_kpis_use_metric_specific_visual_grammars():
    assert 'metricMiniChartHtml(points,metric="value",tone="accent")' in PANEL
    assert 'metric==="costs"||metric==="assetFees"' in PANEL
    assert 'class="kpi-event-baseline"' in PANEL
    assert '(metric==="pnl"||metric==="realized")&&min<=0&&max>=0' in PANEL
    assert 'kind==="step"?raw.flatMap' in PANEL
    assert 'data-kpi-chart="${esc(metric)}"' in PANEL


def test_summary_metrics_open_the_matching_history_mode_directly():
    for metric in ("value", "invested", "costs", "assetFees", "costBasis", "pnl"):
        assert f'data-history-metric="{metric}"' in PANEL
    assert 'requestedMetric=null' in PANEL
    assert 'metricEl.dataset.historyMetric' in PANEL
    assert 'e.target.closest("[data-history-metric]")' in PANEL


def test_dashboard_transaction_kpis_share_exact_ledger_timeline():
    assert 'exact=this.ledgerMetricSeries(events,bounds.start,bounds.end)' in PANEL
    assert 'assetPrincipal:exact.invested' in PANEL
    assert 'otherCosts:exact.costs' in PANEL
    assert 'assetFees:exact.assetFees' in PANEL
    assert 'costBasis:exact.costBasis' in PANEL


def test_dashboard_mini_charts_use_time_proportional_spacing():
    assert 'const xFor=index=>pad+((times[index]-firstTs)/timeSpan)*(w-pad*2);' in PANEL
