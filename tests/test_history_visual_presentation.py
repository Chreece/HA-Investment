from pathlib import Path

PANEL = (Path(__file__).resolve().parents[1] / "custom_components" / "investment" / "www" / "investment-panel.js").read_text(encoding="utf-8")


def test_history_card_has_professional_value_summary_and_stats():
    assert 'class="trend-pop history-card' in PANEL
    assert 'class="history-primary"' in PANEL
    assert 'class="history-stats"' in PANEL
    assert "historyHigh" in PANEL
    assert "historyLow" in PANEL


def test_history_chart_uses_area_grid_and_visible_axis_context():
    assert "trend-area" in PANEL
    assert 'class="trend-grid-line"' in PANEL
    assert 'class="trend-y-label top"' in PANEL
    assert 'class="trend-x-labels"' in PANEL
    assert 'history-spark ${seriesClass}' in PANEL


def test_history_card_remains_pointer_interactive():
    assert ".trend-pop.preview{pointer-events:auto}" in PANEL
    assert "data-trend-tooltip-change" in PANEL
    assert "showTrendPoint(chart,globalIndex,clientX=null,clientY=null)" in PANEL
