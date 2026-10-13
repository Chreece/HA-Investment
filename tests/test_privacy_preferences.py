"""Privacy mode is opt-in and configurable per Home Assistant user."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "investment"


def test_remote_privacy_defaults_off_in_storage():
    text = (COMPONENT / "storage.py").read_text()
    assert '"remote_privacy": False' in text
    assert 'if "remote_privacy" not in user:' in text
    assert 'user["remote_privacy"] = False' in text


def test_remote_privacy_is_a_persisted_preference():
    storage = (COMPONENT / "storage.py").read_text()
    manager = (COMPONENT / "manager.py").read_text()
    websocket = (COMPONENT / "websocket.py").read_text()

    assert "remote_privacy: bool | None = None" in storage
    assert 'user["remote_privacy"] = bool(remote_privacy)' in storage
    assert "remote_privacy: bool | None = None" in manager
    assert "remote_privacy=remote_privacy" in manager
    assert 'vol.Optional("remote_privacy"): bool' in websocket
    assert 'remote_privacy=msg.get("remote_privacy")' in websocket


def test_portfolio_payload_carries_remote_privacy_preference():
    manager = (COMPONENT / "manager.py").read_text()
    assert manager.count('"remote_privacy": bool(user.get("remote_privacy", False))') >= 2


def test_remote_connections_only_auto_hide_when_user_enabled_it():
    panel = (COMPONENT / "www" / "investment-panel.js").read_text()
    assert "_privacyControlsEnabled=portfolio?.privacy_controls_enabled===true" in panel
    assert "_remotePrivacyEnabled=this._privacyControlsEnabled&&portfolio?.remote_privacy===true" in panel
    assert "_remotePrivacyDefault=this._privacyControlsEnabled&&!this._connectionLocal&&this._remotePrivacyEnabled" in panel
    assert "_remotePrivacyDefault=portfolio?.connection_local===false" not in panel


def test_settings_expose_both_privacy_controls():
    panel = (COMPONENT / "www" / "investment-panel.js").read_text()
    assert 'id="privacy-mode-enabled"' in panel
    assert 'id="remote-privacy-enabled"' in panel
    assert 'async savePrivacyMode(enabled)' in panel
    assert 'async saveRemotePrivacy(enabled)' in panel
    assert 'remote_privacy:next' in panel
