from pathlib import Path

PANEL = (Path(__file__).resolve().parents[1] / "custom_components" / "investment" / "www" / "investment-panel.js").read_text(encoding="utf-8")


def test_category_and_holding_cards_use_local_ledger_visual_series():
    assert 'cardLedgerSeries(scope,id,period="1m")' in PANEL
    assert 'const ledgerSeries=this.cardLedgerSeries("category",c.category);' in PANEL
    assert 'const ledgerSeries=this.cardLedgerSeries("holding",h.id);' in PANEL
    assert 'this.trendScopeLedgerEvents({scope,id,period})' in PANEL
    assert 'this.ledgerMetricSeries(events,bounds.start,bounds.end)' in PANEL


def test_card_visuals_do_not_add_provider_history_requests():
    helper_start = PANEL.index('cardLedgerSeries(scope,id,period="1m")')
    helper_end = PANEL.index('changeMeterHtml(percent,tone="neutral")', helper_start)
    helper = PANEL[helper_start:helper_end]
    assert 'this.call(' not in helper
    assert 'investment/history' not in helper


def test_category_cost_metrics_get_metric_specific_micro_charts():
    assert 'metricMiniChartHtml(ledgerSeries.invested,"invested","accent")' in PANEL
    assert 'metricMiniChartHtml(ledgerSeries.costs,"costs","warning")' in PANEL
    assert 'metricMiniChartHtml(ledgerSeries.assetFees,"assetFees","warning")' in PANEL
    assert 'metricMiniChartHtml(ledgerSeries.costBasis,"costBasis","accent")' in PANEL


def test_holding_summary_promotes_principal_and_asset_fee_visuals():
    assert '${esc(this.t("principalSpent"))}<b>${this.money(h.asset_principal)}</b>' in PANEL
    assert '${esc(this.t("assetFees"))}<b>${this.money(h.asset_fee_value)}</b>' in PANEL
    assert 'class="visual-ledger-cell" data-history-metric="invested"' in PANEL
    assert 'class="visual-ledger-cell" data-history-metric="assetFees"' in PANEL


def test_today_and_pnl_use_zero_centered_change_meters():
    assert 'changeMeterHtml(percent,tone="neutral")' in PANEL
    assert 'left=value<0?50-magnitude:50' in PANEL
    assert 'this.changeMeterHtml(c.today_pct,this.signClass(c.today_change))' in PANEL
    assert 'this.changeMeterHtml(c.pnl_pct,this.signClass(c.pnl))' in PANEL
    assert 'this.changeMeterHtml(h.today_pct,this.signClass(h.today_change))' in PANEL
    assert 'this.changeMeterHtml(h.pnl_pct,this.signClass(h.pnl))' in PANEL


def test_card_microcharts_are_compact_and_incognito_compatible():
    assert '.visual-ledger-cell .kpi-spark{height:22px' in PANEL
    assert '.incognito .kpi-spark:not([data-incognito-revealed])' in PANEL
