"""Responsive panel layout uses the available Home Assistant viewport."""
from pathlib import Path

PANEL = Path(__file__).resolve().parents[1] / "custom_components/investment/www/investment-panel.js"


def test_panel_page_is_not_capped_to_fixed_desktop_width():
    text = PANEL.read_text()
    assert ".page{width:100%;max-width:none;" in text
    assert "max-width:1450px" not in text


def test_panel_keeps_mobile_padding_breakpoint():
    text = PANEL.read_text()
    assert ".page{padding:16px 12px 56px}" in text
