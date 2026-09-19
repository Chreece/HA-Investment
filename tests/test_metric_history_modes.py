from pathlib import Path

PANEL = (Path(__file__).resolve().parents[1] / "custom_components" / "investment" / "www" / "investment-panel.js").read_text(encoding="utf-8")


def test_history_analysis_exposes_metric_specific_modes():
    assert "trendMetricPoints(tr=this._trend)" in PANEL
    assert '["value","costBasis","invested","costs","assetFees","pnl"]' in PANEL
    assert 'data-trend-metric="${key}"' in PANEL
    assert "trendMetricLabel(metric=this._trend?.metric)" in PANEL


def test_history_metrics_are_reconstructed_from_scope_ledger():
    assert "trendScopeHoldings(tr=this._trend)" in PANEL
    assert "trendScopeLedgerEvents(tr=this._trend)" in PANEL
    assert "allocated_cost_basis" in PANEL
    assert "realized_pnl" in PANEL
    assert "cash_principal" in PANEL
    assert "asset_fee_value" in PANEL


def test_transaction_driven_metrics_use_step_presentation():
    assert '["costBasis","invested","costs","assetFees"].includes(metric)?"step":"area"' in PANEL
    assert 'kind==="step"?raw.flatMap' in PANEL


def test_pnl_history_has_semantic_zero_reference_and_tooltip_tone():
    assert "trend-zero-line" in PANEL
    assert 'if(metric==="pnl")return this.signClass(value)' in PANEL
    assert "const all=this.trendMetricPoints(tr)" in PANEL


def test_period_reload_preserves_selected_metric():
    assert 'String(this._trend.metric||"value")' in PANEL
