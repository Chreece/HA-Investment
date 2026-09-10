from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "investment"


def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_per_user_indication_preferences_include_all_runtime_criteria():
    tree = ast.parse(_text("const.py"))
    prefs = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "DEFAULT_INDICATION_PREFERENCES"
            for target in node.targets
        ):
            prefs = ast.literal_eval(node.value)
            break
    assert prefs is not None
    assert prefs["portfolio_context"] == "use"
    assert prefs["existing_instruments"] == "allow"
    for key in (
        "scope", "mode", "amount", "category", "ai_task_entity_id",
        "risk_tolerance", "horizon", "strategy", "overlap_policy",
        "overlap_threshold_pct", "diversification", "max_candidate_pct",
        "min_confidence_pct", "min_cash_reserve_pct", "whole_units_only",
        "whole_unit_categories",
    ):
        assert key in prefs


def test_analysis_persists_portfolio_context_and_existing_policy():
    body = _text("manager.py")
    assert '"portfolio_context": portfolio_context, "existing_instruments": existing_instruments' in body
    assert 'values["portfolio_context"] not in {"use", "ignore"}' in body
    assert 'values["existing_instruments"] not in {"allow", "exclude"}' in body


def test_ai_prompt_uses_actual_ui_response_language():
    body = _text("manager.py")
    assert "Respond in the user's UI language" in body
    runtime = _text("runtime_websocket.py")
    assert 'vol.Optional("response_language")' in runtime
    assert '"response_language": msg.get("response_language")' in runtime
    assert 'response_language=progress_callback' not in runtime
    panel = _text("www/investment-panel-runtime.js")
    assert "response_language:langFor(this)" in panel


def test_full_ai_invalid_ranking_falls_back_to_validated_deterministic_allocation():
    body = _text("manager.py")
    assert "def _ai_ranking_usable" in body
    assert "def _deterministic_ai_fallback" in body
    assert 'mirrored["fallback_to_deterministic"] = True' in body
    assert 'ai_review["fallback_to_deterministic"] = True' in body


def test_hover_help_bubbles_cover_every_indication_input():
    panel = _text("www/investment-panel-runtime.js")
    for name in (
        "indication_amount", "indication_scope", "indication_category",
        "indication_whole_unit_category", "indication_risk", "indication_horizon",
        "indication_mode", "indication_ai_task", "indication_strategy",
        "indication_overlap", "indication_overlap_threshold",
        "indication_diversification", "indication_max_candidate",
        "indication_min_confidence", "indication_cash_reserve",
        "indication_portfolio_context", "indication_new_only",
    ):
        assert name in panel
    assert "field-help" in panel
    assert "help-label" in panel
    assert '.help-label:hover>.field-help' not in panel
    assert 'pointerType==="mouse"' in panel
    assert '.field-help{display:none!important}' in panel
    assert 'document.body.appendChild(portal)' in panel


def test_result_terms_have_help_and_friendly_labels():
    panel = _text("www/investment-panel-runtime.js")
    for key in (
        "marketScore", "portfolioFit", "dataReliability", "economicSleeve",
        "r63", "r126", "rsi", "volatility", "currentDrawdown",
        "maxDrawdown", "relativeStrength", "riskEnvelope",
        "expectedShortfall", "activation", "classification",
    ):
        assert key in panel
    assert "Validated diversified model" in panel
    assert "Επικυρωμένο διαφοροποιημένο μοντέλο" in panel


def test_ai_output_is_markdown_rendered_and_structured_data_is_not_raw_json_only():
    panel = _text("www/investment-panel-runtime.js")
    assert "function markdownHtml" in panel
    assert "function structuredHtml" in panel
    assert "ai-structured-object" in panel
    assert "ai-ranking-list" in panel
    assert "technicalDetails" in panel


def test_explanatory_classification_metadata_does_not_replace_validated_math():
    body = _text("manager.py")
    assert "def _economic_classification_metadata" in body
    assert '"economic_classification_confidence"' in body
    assert '"product_structure_flags"' in body
    assert "These fields do not alter the frozen V13/V12c" in body
