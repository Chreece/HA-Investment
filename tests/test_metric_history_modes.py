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


def test_transaction_driven_metrics_use_distinct_history_presentations():
    assert 'if(metric==="costs"||metric==="assetFees")return "events";' in PANEL
    assert 'if(metric==="costBasis"||metric==="invested")return "step";' in PANEL
    assert 'if(metric==="pnl")return "divergence";' in PANEL
    assert 'kind==="step"?raw.flatMap' in PANEL
    assert 'class="trend-event-bar"' in PANEL
    assert "trendEventDeltas(points=[])" in PANEL


def test_pnl_history_has_semantic_zero_reference_and_divergence_fill():
    assert "trend-zero-line" in PANEL
    assert 'if(metric==="pnl")return this.signClass(value)' in PANEL
    assert "investment-pnl-line" in PANEL
    assert "investment-pnl-area" in PANEL
    assert "const all=this.trendMetricPoints(tr)" in PANEL


def test_cost_and_fee_history_tooltips_snap_to_real_events():
    assert 'const deltas=this.trendMetricKind(tr.metric)==="events"?this.trendEventDeltas(win.points):null;' in PANEL
    assert 'if(deltas&&!(Number(deltas[index]?.value)>1e-12))return;' in PANEL
    assert 'kind==="events"?plottedValue:value' in PANEL
    assert 'Σ ${this.money(value,tr.currency)}' in PANEL
    assert "historyLargestEvent" in PANEL
    assert "historyEvents" in PANEL


def test_period_reload_preserves_selected_metric():
    assert 'String(this._trend.metric||"value")' in PANEL


def test_transaction_metrics_use_exact_ledger_timestamps():
    assert 'historyTimeBounds(period="1m",marketPoints=[],events=[])' in PANEL
    assert 'ledgerMetricSeries(events=[],startTs=0,endTs=Math.floor(Date.now()/1000))' in PANEL
    assert 'Number(sorted[index].sort_ts)<startTs' in PANEL
    assert 'while(index<sorted.length&&Number(sorted[index].sort_ts)===ts)' in PANEL
    assert 'push(ts);' in PANEL
    assert 'this.ledgerMetricSeries(events,bounds.start,bounds.end)[metric]' in PANEL


def test_history_chart_and_pointer_use_time_proportional_x_axis():
    assert '((Number(point.ts)-firstTs)/timeSpan)*(w-padX*2)' in PANEL
    assert 'targetTs=firstTs+frac*Math.max(1,lastTs-firstTs)' in PANEL
    assert 'Math.abs(Number(point.ts)-targetTs)' in PANEL


def test_holding_history_adds_price_units_realized_and_unrealized_only_for_holdings():
    assert 'trendSupportedMetrics(tr=this._trend)' in PANEL
    assert 'if(tr?.scope!=="holding")return base;' in PANEL
    for metric in ("price", "quantity", "realized", "unrealized"):
        assert f'["{metric}",this.t(' in PANEL
    assert 'this.trendSupportedMetrics(tr).some(([key])=>key===metric)' in PANEL


def test_quantity_and_realized_histories_are_exact_ledger_steps():
    assert 'quantity:0' in PANEL
    assert 'state.quantity+=Math.max(0,qty)' in PANEL
    assert 'state.quantity=Math.max(0,state.quantity-Math.max(0,qty))' in PANEL
    assert 'if(metric==="quantity")return state.quantity;' in PANEL
    assert 'if(metric==="realized")return state.realized;' in PANEL
    assert 'quantity:[],realized:[]' in PANEL
    assert 'if(metric==="realized")return "divergenceStep";' in PANEL


def test_price_and_unrealized_history_use_market_samples_with_ledger_state():
    assert 'if(metric==="price")value=state.quantity>1e-12?point.value/state.quantity:null;' in PANEL
    assert 'else if(metric==="unrealized")value=state.costKnown?point.value-state.costBasis:null;' in PANEL
    assert 'if(metric==="pnl"||metric==="unrealized")return "divergence";' in PANEL


def test_history_formatter_supports_units_price_and_signed_pnl():
    assert 'trendMetricFormat(value,tr=this._trend)' in PANEL
    assert 'if(metric==="quantity")' in PANEL
    assert 'if(metric==="price")return this.price(value,tr?.currency);' in PANEL
    assert '["pnl","realized","unrealized"].includes(metric)' in PANEL
