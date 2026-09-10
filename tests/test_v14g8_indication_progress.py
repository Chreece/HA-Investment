from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "investment"


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_manager_reports_real_analysis_stages_and_candidate_completion():
    manager = text("manager.py")
    assert "progress_callback: Callable[[int, str, dict[str, Any] | None], None] | None = None" in manager
    for percent, stage in (
        (5, "starting"),
        (10, "portfolio_context"),
        (22, "candidate_search"),
        (31, "candidate_checks"),
        (35, "market_history"),
        (76, "scoring"),
        (82, "risk_allocation"),
        (92, "ai_review"),
        (98, "finalizing"),
    ):
        assert f'emit_progress({percent}, "{stage}"' in manager
    assert "async def evaluate_with_progress" in manager
    assert "completed_evaluations += 1" in manager
    assert "38 * completed_evaluations / total" in manager
    assert 'completed=completed_evaluations' in manager
    assert 'total=len(eligible_assets)' in manager


def test_runtime_manager_forwards_progress_without_touching_model_math():
    runtime_manager = text("runtime_manager.py")
    assert "progress_callback: Callable[[int, str, dict[str, Any] | None], None] | None = None" in runtime_manager
    assert 'progress_callback(3, "starting", None)' in runtime_manager
    assert 'except Exception as err:' in runtime_manager
    assert "progress_callback=progress_callback" in runtime_manager


def test_websocket_keeps_legacy_call_and_adds_owned_progress_jobs():
    websocket = text("runtime_websocket.py")
    assert '_indication_schema("investment/indication")' in websocket
    assert '_indication_schema("investment/indication_start")' in websocket
    assert 'vol.Required("type"): "investment/indication_status"' in websocket
    assert '"user_id": user_id' in websocket
    assert 'str(job.get("user_id") or "") != base._user_id(connection)' in websocket
    assert 'hass.async_create_task(run_job())' in websocket
    assert 'async def _run_indication(\n    hass: HomeAssistant,\n    user_id: str,' in websocket
    assert 'result = await _run_indication(hass, user_id, msg)' in websocket
    assert 'jobs.pop(job_id, None)' in websocket
    assert 'current.update(\n                        percent=100,\n                        stage="complete"' in websocket


def test_progress_job_percent_is_monotonic_and_bounded_server_side():
    websocket = text("runtime_websocket.py")
    assert 'max(int(current.get("percent") or 0), min(99, int(percent)))' in websocket
    assert '"percent": int(job.get("percent") or 0)' in websocket
    assert '"done": bool(job.get("done"))' in websocket


def test_frontend_uses_start_and_status_progress_protocol():
    panel = text("www/investment-panel.js")
    run_block = panel.split("async runIndication(form){", 1)[1].split("indicationModalHtml(){", 1)[0]
    assert 'type:"investment/indication_start"' in run_block
    assert 'this.waitForIndicationJob(jobId,runSeq)' in run_block
    wait_block = panel.split("async waitForIndicationJob(jobId,runSeq){", 1)[1].split("async runIndication(form){", 1)[0]
    assert 'type:"investment/indication_status"' in wait_block
    assert 'setTimeout(resolve,250)' in wait_block
    assert 'this.setIndicationProgress(status)' in wait_block


def test_progress_overlay_is_obvious_accessible_and_outside_scroll_geometry():
    panel = text("www/investment-panel.js")
    assert 'class="indication-progress-overlay" role="status" aria-live="polite"' in panel
    assert 'role="progressbar" aria-valuemin="0" aria-valuemax="100"' in panel
    assert 'data-indication-progress-percent>${percent}%' in panel
    css_start = panel.index(".indication-progress-overlay{")
    css = panel[css_start:css_start + 1900]
    assert "position:fixed" in css
    assert "inset:0" in css
    assert "place-items:center" in css
    assert "font-size:30px" in css
    assert "height:10px" in css
    assert "overflow:auto" not in css


def test_progress_copy_covers_every_supported_panel_language():
    panel = text("www/investment-panel.js")
    block = panel.split("const INDICATION_PROGRESS_I18N = {", 1)[1].split("};\nfor (const [lang, values]", 1)[0]
    expected = {
        "en", "de", "el", "fr", "es", "it", "pt", "nl", "pl", "tr", "ru", "uk",
        "cs", "hu", "ro", "sv", "da", "fi", "no", "ja", "ko", "zh", "ar", "bg",
        "sk", "he", "hi", "id",
    }
    present = set(re.findall(r"^  ([a-z]{2}):\{", block, re.M))
    assert present == expected
    for lang in expected:
        row = next(line for line in block.splitlines() if line.startswith(f"  {lang}:"))
        for key in (
            "title", "starting", "portfolio_context", "candidate_search", "candidate_checks",
            "market_history", "scoring", "risk_allocation", "ai_review", "finalizing",
            "complete", "checked",
        ):
            assert f"{key}:" in row, (lang, key)


def test_runtime_enhancement_fields_are_forwarded_to_progress_start_call():
    runtime = text("www/investment-panel-runtime.js")
    assert '["investment/indication","investment/indication_start"].includes(message.type)' in runtime
    assert 'portfolio_context:' in runtime
    assert 'existing_instruments:' in runtime
    assert 'response_language:' in runtime


def test_v14g8_uses_fresh_progress_panel_identity():
    const = text("const.py")
    runtime = text("www/investment-panel-runtime.js")
    panel = text("www/investment-panel.js")
    assert 'PANEL_ASSET_REVISION = "0.4.0-r36"' in const
    assert 'PANEL_NAME = "investment-panel-r36"' in const
    assert 'import "./investment-panel.js?v=0.4.0-r36";' in runtime
    assert 'const Panel = customElements.get("investment-panel-r36")' in runtime
    assert 'customElements.define("investment-panel-r36",InvestmentPanel)' in panel
