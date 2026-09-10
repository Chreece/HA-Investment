from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "investment"


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_help_requires_real_hover_capability_before_desktop_motion_can_open():
    runtime = text("www/investment-panel-runtime.js")
    assert 'const coarsePointer=()=>!!window.matchMedia?.("(hover:none), (pointer:coarse)")?.matches;' in runtime
    assert 'const trueHoverMouse=()=>!!window.matchMedia?.("(hover:hover) and (pointer:fine)")?.matches;' in runtime
    assert 'const isMouseHover=e=>e?.pointerType==="mouse"&&trueHoverMouse()&&!coarsePointer();' in runtime
    assert 'target.addEventListener("pointerenter"' in runtime
    assert 'target.addEventListener("pointermove"' in runtime
    assert 'data-help-hover-ready' in runtime


def test_touch_and_desktop_help_share_document_body_portal_outside_scroll_containers():
    runtime = text("www/investment-panel-runtime.js")
    assert 'const HELP_PORTAL_ID="ha-investment-help-portal";' in runtime
    assert 'document.body.appendChild(portal)' in runtime
    assert '.field-help{display:none!important}' in runtime
    assert 'position:fixed' in runtime
    portal_style = runtime.split('style.textContent=`', 1)[1].split('`;', 1)[0]
    assert 'overflow:auto' not in portal_style
    assert 'overflow-y:auto' not in portal_style
    assert 'max-height:' not in portal_style


def test_touch_help_can_always_be_dismissed_by_portal_or_outside_tap():
    runtime = text("www/investment-panel-runtime.js")
    assert 'helpPortal.onclick=e=>{e.preventDefault();e.stopPropagation();closeHelp(null);};' in runtime
    assert 'else closeHelp(null);return;}closeHelp(null);},true);' in runtime


def test_v14g7_uses_fresh_component_identity():
    const = text("const.py")
    runtime = text("www/investment-panel-runtime.js")
    base = text("www/investment-panel.js")
    assert 'PANEL_ASSET_REVISION = "0.4.0-r36"' in const
    assert 'PANEL_NAME = "investment-panel-r36"' in const
    assert 'import "./investment-panel.js?v=0.4.0-r36";' in runtime
    assert 'const Panel = customElements.get("investment-panel-r36")' in runtime
    assert 'customElements.define("investment-panel-r36",InvestmentPanel)' in base
