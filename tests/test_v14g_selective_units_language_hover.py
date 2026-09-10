from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "investment"

def text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")

def test_whole_unit_categories_are_persisted_per_user():
    tree = ast.parse(text("const.py"))
    prefs = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "DEFAULT_INDICATION_PREFERENCES" for t in node.targets):
            prefs = ast.literal_eval(node.value)
            break
    assert prefs is not None
    assert prefs["whole_unit_categories"] == []
    manager = text("manager.py")
    assert 'values["whole_unit_categories"] = whole_categories' in manager
    assert '"whole_unit_categories": list(whole_unit_categories)' in manager

def test_selective_whole_units_are_enforced_in_projection_and_ai_clamp():
    model = text("validated_model.py")
    assert "def _whole_unit_required" in model
    assert "whole_unit_categories: Iterable[str] | None = None" in model
    assert model.count("whole_unit_categories=whole_categories") >= 2
    runtime = text("runtime_websocket.py")
    assert 'vol.Optional("whole_unit_categories", default=[])' in runtime
    assert '"whole_unit_categories": list(msg.get("whole_unit_categories") or [])' in runtime

def test_frontend_uses_multiselect_for_selective_integer_units():
    panel = text("www/investment-panel-runtime.js")
    assert "WHOLE_UNIT_CATEGORIES" in panel
    assert "wholeUnitMultiHtml" in panel
    assert 'name="indication_whole_unit_category"' in panel
    assert "whole_unit_categories" in panel
    assert "wholeUnitSummary" in panel

def test_explanations_are_title_hover_or_focus_bubbles_without_question_button():
    panel = text("www/investment-panel-runtime.js")
    assert 'class="help-label"' in panel
    assert '.help-label:hover>.field-help' not in panel
    assert '.help-label:focus-visible>.field-help' not in panel
    assert 'const isMouseHover=e=>e?.pointerType==="mouse"&&trueHoverMouse()&&!coarsePointer();' in panel
    assert 'pointerenter' in panel
    assert 'aria-label="Info">?</summary>' not in panel
    assert "help-open" in panel  # touch/mobile tap fallback

def test_ai_language_is_enforced_with_one_retry_and_safe_display_fallback():
    manager = text("manager.py")
    assert "def _ai_language_matches" in manager
    assert "def _retry_ai_language" in manager
    assert "ALL natural-language output MUST be in" in manager
    assert '"language_retry_used": language_retry_used' in manager
    assert '"language_mismatch": language_mismatch' in manager
    panel = text("www/investment-panel-runtime.js")
    assert "aiLanguageFallback" in panel
    assert "display_structured" in panel
    assert "display_text" in panel


def test_selective_whole_unit_helper_marks_only_selected_wrapper_type():
    import importlib.util
    spec = importlib.util.spec_from_file_location("validated_model_v14g", ROOT / "validated_model.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    selected = module._whole_unit_category_set(["etf"])
    assert module._whole_unit_required({"category": "etf"}, False, selected) is True
    assert module._whole_unit_required({"category": "stock"}, False, selected) is False
    assert module._whole_unit_required({"category": "stock"}, True, selected) is True


def test_frontend_selective_unit_display_and_empty_state_use_per_item_requirement():
    panel = text("www/investment-panel.js")
    assert "!!item?.whole_units_only" in panel
    assert "whole_unit_categories" in panel
    runtime = text("www/investment-panel-runtime.js")
    assert "whole_unit_categories)&&p.whole_unit_categories.length" in runtime


def test_v14g5_bumps_runtime_asset_revision_and_component_identity():
    const = text("const.py")
    runtime = text("www/investment-panel-runtime.js")
    assert 'PANEL_ASSET_REVISION = "0.4.0-r36"' in const
    assert 'investment-panel.js?v=0.4.0-r36' in runtime
    assert 'PANEL_NAME = "investment-panel-r36"' in const


def test_selective_whole_units_change_literal_output_without_forcing_other_types():
    import importlib.util
    spec = importlib.util.spec_from_file_location("validated_model_v14g_discrete", ROOT / "validated_model.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    weekly = {f"2025-W{i:02d}": 0.001 for i in range(1, 53)}
    weekly.update({f"2026-W{i:02d}": 0.001 for i in range(1, 10)})
    rows = [
        {"symbol": "ETF", "category": "etf", "economic_sleeve": "broad_equity", "portfolio_price": 30.0, "suggested_amount": 55.0, "suggested_units": 55/30, "risk_weekly_returns": weekly},
        {"symbol": "STK", "category": "stock", "economic_sleeve": "single_equity", "portfolio_price": 30.0, "suggested_amount": 45.0, "suggested_units": 1.5, "risk_weekly_returns": weekly},
    ]
    out, meta = module.enforce_discrete_risk_contract(
        rows, "very_high", 100.0, whole_unit_categories=["etf"]
    )
    by_symbol = {row["symbol"]: row for row in out}
    assert by_symbol["ETF"]["whole_units_only"] is True
    assert by_symbol["ETF"]["suggested_units"] == 1.0
    assert by_symbol["ETF"]["suggested_amount"] == 30.0
    assert by_symbol["STK"]["whole_units_only"] is False
    assert by_symbol["STK"]["suggested_units"] == 1.5
    assert by_symbol["STK"]["suggested_amount"] == 45.0
    assert meta["risk_scale"] == 1.0


def test_ai_normal_view_localizes_known_structured_field_labels():
    panel = text("www/investment-panel-runtime.js")
    assert "function aiKeyLabel" in panel
    assert 'el:"Καλύτερη αντιστοίχιση"' in panel
    assert 'el:"Επόμενα βήματα"' in panel
    assert "panel.indicationSignalText(row.action" in panel


def test_runtime_manager_accepts_and_forwards_selective_whole_unit_categories():
    runtime_manager = text("runtime_manager.py")
    assert "whole_unit_categories: list[str] | None = None" in runtime_manager
    assert "whole_unit_categories=whole_unit_categories" in runtime_manager
