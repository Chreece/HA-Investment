from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components" / "investment"


def text(path: str) -> str:
    return (COMP / path).read_text(encoding="utf-8")


def test_result_score_help_cannot_auto_open_from_css_focus_or_touch_hover():
    runtime = text("www/investment-panel-runtime.js")
    assert '.help-label:hover>.field-help' not in runtime
    assert '.help-label:focus>.field-help' not in runtime
    assert '.help-label:focus-visible>.field-help' not in runtime
    assert 'const isMouseHover=e=>e?.pointerType==="mouse"&&trueHoverMouse()&&!coarsePointer();' in runtime
    assert 'target.addEventListener("pointerenter"' in runtime
    assert 'target.setAttribute("data-help-hover-ready","1")' in runtime
    assert 'target.addEventListener("pointermove"' in runtime
    assert 'target.classList.remove("help-open","help-touch")' in runtime
    # All help uses one document-level portal, never a nested scrolling bubble.
    assert '.field-help{display:none!important}' in runtime
    assert 'const HELP_PORTAL_ID="ha-investment-help-portal";' in runtime
    assert 'document.body.appendChild(portal)' in runtime
    portal_style = runtime.split('style.textContent=`',1)[1].split('`;',1)[0]
    assert 'overflow:auto' not in portal_style
    assert '.indication-modal,.indication-results{min-width:0;max-width:100%;overflow-x:hidden!important}' in runtime


def test_every_visible_reason_warning_and_blocker_has_plain_help_in_en_de_el():
    runtime = text("www/investment-panel-runtime.js")
    source = "\n".join(
        (COMP / name).read_text(encoding="utf-8")
        for name in ("indication.py", "validated_model.py")
    )
    emitted = set(
        re.findall(r'(?:reasons|warnings|blockers)\.append\("([a-z0-9_]+)"\)', source)
    )
    signal_block = runtime.split("const SIGNAL = {", 1)[1].split("const SIGNAL_HELP = {", 1)[0]
    help_block = runtime.split("const SIGNAL_HELP = {", 1)[1].split("const PLAIN = {", 1)[0]
    assert emitted
    for code in emitted:
        # one display label + one explanation in each audited UI language
        assert signal_block.count(f"{code}:") == 3, code
        assert help_block.count(f"{code}:") == 3, code
    assert 'reasons.innerHTML=signalItemsHtml(this,item.reasons||[],{warning:false})' in runtime
    assert 'warnings.innerHTML=signalItemsHtml(this,item.warnings||[],{warning:true})' in runtime
    assert 'signalHelpBubble(panel,code)' in runtime


def test_technical_glossary_covers_every_help_topic_exactly_once():
    runtime = text("www/investment-panel-runtime.js")
    help_block = runtime.split("const HELP = {", 1)[1].split("const SIGNAL = {", 1)[0]
    help_keys = set(re.findall(r'^    ([A-Za-z0-9_]+):', help_block, re.M))
    groups = runtime.split("const GLOSSARY_GROUPS=[", 1)[1].split("];\nfunction glossaryHtml", 1)[0]
    quoted = re.findall(r'"([A-Za-z0-9_]+)"', groups)
    group_names = {"groupSettings", "groupResults", "groupRisk", "groupInside"}
    glossary_keys = [value for value in quoted if value not in group_names]
    assert len(glossary_keys) == len(set(glossary_keys))
    assert set(glossary_keys) == help_keys
    # HELP itself must have a separate explanation in all three audited languages.
    for key in help_keys:
        assert help_block.count(f"{key}:") == 3, key


def test_algorithm_explainer_is_plain_two_stage_and_visible_before_results():
    runtime = text("www/investment-panel-runtime.js")
    assert "What happens when you press Analyze?" in runtime
    assert "History first, then your safety rules" in runtime
    assert "Τι γίνεται όταν πατάς Ανάλυση;" in runtime
    assert "Πρώτα το ιστορικό, μετά οι κανόνες ασφάλειάς σου" in runtime
    assert "Was passiert beim Klick auf Analysieren?" in runtime
    assert "Erst die Historie, dann deine Sicherheitsregeln" in runtime
    assert "A high score is not a buy command" in runtime
    assert "Υψηλό σκορ δεν σημαίνει «αγόρασε»" in runtime
    assert 'intro.insertAdjacentHTML("afterend",algorithmExplainerHtml(this))' in runtime


def test_plain_language_definitions_explicitly_block_common_financial_misreadings():
    runtime = text("www/investment-panel-runtime.js")
    for phrase in (
        "It is not a probability of profit",
        "It does not mean the data provider is 100% correct",
        "This is not the same thing as RSI",
        "not a statement that the investment will lose 20%",
        "This is classification confidence, not confidence that the investment will perform well",
        "future returns are never guaranteed",
    ):
        assert phrase in runtime
    for phrase in (
        "Δεν είναι πιθανότητα κέρδους",
        "δεν σημαίνει 100% πιθανότητα κέρδους",
        "δεν προβλέπουν το μέλλον",
    ):
        assert phrase in runtime


def test_allocation_explanation_rows_get_the_same_help_system():
    runtime = text("www/investment-panel-runtime.js")
    for key in (
        "deploymentWhy", "deploymentTarget", "strongestEligible",
        "riskDeploymentMultiplier", "capReducedTarget", "wholeUnitAdjustment",
    ):
        assert f'helpBubble(this,"{key}")' in runtime or f'keys.push("{key}")' in runtime
    assert 'const allocationExplanation=modal.querySelector(".allocation-explanation")' in runtime


def test_v14g5_fresh_component_identity_for_mobile_webview():
    const = text("const.py")
    runtime = text("www/investment-panel-runtime.js")
    base = text("www/investment-panel.js")
    assert 'PANEL_ASSET_REVISION = "0.4.0-r36"' in const
    assert 'PANEL_NAME = "investment-panel-r36"' in const
    assert 'import "./investment-panel.js?v=0.4.0-r36";' in runtime
    assert 'const Panel = customElements.get("investment-panel-r36")' in runtime
    assert 'customElements.define("investment-panel-r36",InvestmentPanel)' in base
    assert 'await import(' not in runtime
