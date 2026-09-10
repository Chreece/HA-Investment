from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_v15_panel_wrapper_is_wired_without_changing_existing_revision():
    const = read("custom_components/investment/const.py")
    init = read("custom_components/investment/__init__.py")
    wrapper = read("custom_components/investment/www/investment-panel-v15.js")

    assert 'PANEL_ASSET_REVISION = "0.4.0-r36"' in const
    assert 'module_url=f"{STATIC_URL}/investment-panel-v15.js?v={PANEL_ASSET_REVISION}-{runtime_revision}"' in init
    assert 'import "./investment-panel-runtime.js?v=0.4.0-r36";' in wrapper


def test_score_help_is_blocked_until_genuine_mouse_motion():
    wrapper = read("custom_components/investment/www/investment-panel-v15.js")

    assert 'document.addEventListener("pointerenter", (event) =>' in wrapper
    assert 'event.stopPropagation();' in wrapper
    assert 'document.addEventListener("pointermove", (event) =>' in wrapper
    assert 'event?.pointerType !== "mouse"' in wrapper
    assert 'event?.movementX' in wrapper
    assert 'event?.movementY' in wrapper
    assert 'dispatchHelpKey(target, "Enter")' in wrapper
    assert 'dispatchHelpKey(hoveredScore, "Escape")' in wrapper


def test_score_help_native_title_tooltips_are_removed():
    wrapper = read("custom_components/investment/www/investment-panel-v15.js")

    assert 'new MutationObserver' in wrapper
    assert 'removeAttribute("title")' in wrapper
    assert 'attributeFilter: ["title"]' in wrapper


def test_v15_does_not_change_validated_risk_threshold():
    validated = read("custom_components/investment/validated_model.py")
    assert "MIN_RISK_HISTORY_WEEKS = 52" in validated
    assert "MIN_RISK_HISTORY_WEEKS = 156" not in validated
