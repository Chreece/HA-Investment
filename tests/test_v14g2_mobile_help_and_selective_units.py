from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "investment"

def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")

def test_v14g2_base_panel_natively_renders_selective_whole_unit_multiselect():
    panel = text("www/investment-panel.js")
    assert 'name="indication_whole_unit_category"' in panel
    assert 'data-whole-unit-multi' in panel
    assert 'wholeUnitCategoryOptions' in panel
    assert 'whole_unit_categories:Array.isArray(draft.wholeUnitCategories)?draft.wholeUnitCategories:[]' in panel

def test_v14g2_mobile_help_is_tap_only_and_cannot_create_horizontal_overflow():
    runtime = text("www/investment-panel-runtime.js")
    assert 'const isMouseHover=e=>e?.pointerType==="mouse"&&trueHoverMouse()&&!coarsePointer();' in runtime
    assert '.help-label:hover>.field-help' not in runtime
    assert '.field-help{display:none!important}' in runtime
    assert 'const HELP_PORTAL_ID="ha-investment-help-portal";' in runtime
    assert 'document.body.appendChild(portal)' in runtime
    assert 'overflow-x:hidden!important' in runtime
    # Generic focus/hover display must not be the always-on rule used by V14g.1.
    assert '.help-label:focus>.field-help' not in runtime
    assert '.help-label:focus-within>.field-help' not in runtime

def test_v14g2_help_taps_prevent_label_default_and_old_runtime_listener_duplication():
    runtime = text("www/investment-panel-runtime.js")
    assert 'modal.addEventListener("click"' in runtime
    assert 'e.preventDefault();e.stopImmediatePropagation()' in runtime
    assert 'toggleHelp(target' in runtime
    assert 'modal.querySelectorAll(".field-help").forEach(el=>el.remove())' in runtime
    assert 'modal.querySelectorAll("[title]").forEach(el=>el.removeAttribute("title"))' in runtime
    assert 'modal.querySelectorAll(".indication-glossary,.algorithm-explainer").forEach(el=>el.remove())' in runtime
