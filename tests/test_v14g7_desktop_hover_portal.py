from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "investment"


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_new_results_under_stationary_mouse_cannot_open_score_help():
    runtime = text("www/investment-panel-runtime.js")
    # pointerenter only arms the target; real pointer motion over it opens help.
    arm = 'target.addEventListener("pointerenter",e=>{if(isMouseHover(e))target.setAttribute("data-help-hover-ready","1");});'
    move = 'target.addEventListener("pointermove",e=>{if(!isMouseHover(e)||target.getAttribute("data-help-hover-ready")!=="1")return;'
    assert arm in runtime
    assert move in runtime
    assert 'pointerenter",e=>{if(!isMouseHover(e))return;openHelp' not in runtime


def test_desktop_help_cannot_change_modal_scroll_geometry():
    runtime = text("www/investment-panel-runtime.js")
    assert '.field-help{display:none!important}' in runtime
    assert 'document.body.appendChild(portal)' in runtime
    assert 'positionHelpPortal(helpPortal,target,touch)' in runtime
    assert 'position:fixed' in runtime
    assert '.help-label.help-open:not(.help-touch)>.field-help' not in runtime


def test_desktop_help_closes_on_pointer_leave_and_scroll():
    runtime = text("www/investment-panel-runtime.js")
    assert 'target.addEventListener("pointerleave"' in runtime
    assert 'target.getAttribute("data-help-source")==="hover")closeHelp(null)' in runtime
    assert 'modal.addEventListener("scroll",()=>closeHelp(null),{passive:true,capture:true});' in runtime


def test_desktop_mouse_click_does_not_latch_a_hover_bubble():
    runtime = text("www/investment-panel-runtime.js")
    assert 'if(touch)toggleHelp(target,{touch:true,source:"tap"});else closeHelp(null);return;' in runtime
