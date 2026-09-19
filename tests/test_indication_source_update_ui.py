from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPDATE = (ROOT / "custom_components" / "investment" / "update.py").read_text(encoding="utf-8")
PANEL = (ROOT / "custom_components" / "investment" / "www" / "investment-panel.js").read_text(encoding="utf-8")


def test_source_update_entity_keeps_30_minute_poll_and_publishes_diagnostics():
    assert "SCAN_INTERVAL = timedelta(minutes=30)" in UPDATE
    for token in (
        '"investment_source_update": True',
        '"source_repository": _SOURCE_REPOSITORY',
        '"source_ref": _SOURCE_REF',
        '"installed_revision": self._installed_sha',
        '"latest_validated_revision": self._latest_sha',
        '"source_head_revision": self._source_head_sha',
        '"source_head_validated": self._source_head_validated',
        '"last_checked_at": self._last_checked_at',
        '"last_check_success": self._last_check_success',
        '"last_check_error": self._last_check_error',
        '"scan_interval_minutes": int(SCAN_INTERVAL.total_seconds() // 60)',
    ):
        assert token in UPDATE


def test_source_update_check_records_success_failure_and_validation_state():
    assert "def _record_check_success(self, head_sha: str, ready: bool)" in UPDATE
    assert "def _record_check_failure(self, err: Exception)" in UPDATE
    assert "datetime.now(UTC).isoformat()" in UPDATE
    assert "self._record_check_success(head_sha, ready)" in UPDATE
    assert "self._record_check_failure(err)" in UPDATE
    assert "self._source_head_validated = bool(ready)" in UPDATE


def test_settings_find_the_real_update_entity_and_do_not_poll_github_directly():
    assert "sourceUpdateEntity(hass=this._hass)" in PANEL
    assert 'state?.attributes?.investment_source_update===true' in PANEL
    assert 'this._hass.callService("homeassistant","update_entity",{entity_id:found.entityId})' in PANEL
    helper_start = PANEL.index("sourceUpdateEntity(hass=this._hass)")
    helper_end = PANEL.index("async loadPortfolio(", helper_start)
    helper = PANEL[helper_start:helper_end]
    assert "api.github.com" not in helper
    assert "fetch(" not in helper


def test_source_update_settings_show_status_revisions_check_time_and_cadence():
    assert "sourceUpdateHtml()" in PANEL
    assert 'class="source-update-status ${esc(status.tone)}"' in PANEL
    assert "sourceUpdateInstalled" in PANEL
    assert "sourceUpdateLatestValidated" in PANEL
    assert "sourceUpdateBranchHead" in PANEL
    assert "sourceUpdateLastCheck" in PANEL
    assert "sourceUpdateAutomatic" in PANEL
    assert "scan_interval_minutes" in PANEL
    assert "data-source-update-check" in PANEL


def test_source_update_status_distinguishes_failure_pending_available_and_current():
    assert 'a.last_check_success===false' in PANEL
    assert 'a.source_head_validated===false&&a.source_head_revision' in PANEL
    assert 'found.state?.state==="on"' in PANEL
    assert 'sourceUpdateCheckFailed' in PANEL
    assert 'sourceUpdateValidationPending' in PANEL
    assert 'sourceUpdateAvailable' in PANEL
    assert 'sourceUpdateUpToDate' in PANEL


def test_updater_state_changes_only_rebuild_open_settings():
    assert "sourceUpdateSignature(value)" in PANEL
    assert "(sourceUpdateChanged&&this._settings)" in PANEL
