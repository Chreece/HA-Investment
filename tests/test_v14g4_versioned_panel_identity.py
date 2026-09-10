from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "investment"

def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")

def test_backend_and_frontend_use_same_versioned_component_identity():
    const = text("const.py")
    init = text("__init__.py")
    base = text("www/investment-panel.js")
    runtime = text("www/investment-panel-runtime.js")
    assert 'PANEL_NAME = "investment-panel-r36"' in const
    assert "webcomponent_name=PANEL_NAME" in init
    assert 'customElements.get("investment-panel-r36")' in base
    assert 'customElements.define("investment-panel-r36",InvestmentPanel)' in base
    assert 'const Panel = customElements.get("investment-panel-r36")' in runtime

def test_legacy_component_identity_cannot_capture_the_v14g4_panel():
    base = text("www/investment-panel.js")
    runtime = text("www/investment-panel-runtime.js")
    assert 'customElements.define("investment-panel",InvestmentPanel)' not in base
    assert 'const Panel = customElements.get("investment-panel");' not in runtime

def test_versioned_identity_keeps_selective_units_native():
    base = text("www/investment-panel.js")
    assert 'class="setting-field whole-unit-multi"' in base
    assert 'name="indication_whole_unit_category"' in base
    assert 'name="indication_whole_units_only"' not in base
