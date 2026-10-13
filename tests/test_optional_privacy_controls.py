"""Privacy controls are a per-user opt-in feature, not a forced default."""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
from types import ModuleType

import pytest


COMPONENT = Path(__file__).resolve().parents[1] / "custom_components/investment"


class FakeStore:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, *args, **kwargs):
        self.saved = []

    async def async_load(self):
        return None

    async def async_save(self, data):
        self.saved.append(data)


@pytest.fixture(scope="module")
def storage_module():
    patch = pytest.MonkeyPatch()
    package_name = "_investment_privacy_runtime"
    package = ModuleType(package_name)
    package.__path__ = [str(COMPONENT)]
    patch.setitem(sys.modules, package_name, package)

    homeassistant = ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = ModuleType("homeassistant.core")
    core.HomeAssistant = object
    helpers = ModuleType("homeassistant.helpers")
    helpers.__path__ = []
    storage = ModuleType("homeassistant.helpers.storage")
    storage.Store = FakeStore
    for name, module in (
        ("homeassistant", homeassistant),
        ("homeassistant.core", core),
        ("homeassistant.helpers", helpers),
        ("homeassistant.helpers.storage", storage),
    ):
        patch.setitem(sys.modules, name, module)

    loaded = importlib.import_module(f"{package_name}.storage")
    yield loaded
    for name in list(sys.modules):
        if name.startswith(package_name):
            sys.modules.pop(name, None)
    patch.undo()


def make_store(storage_module):
    return storage_module.InvestmentStore(None)


def test_new_user_defaults_privacy_controls_off(storage_module):
    store = make_store(storage_module)
    user = {}
    assert store._normalize_user(user) is True
    assert user["privacy_controls_enabled"] is False
    assert user["incognito"] is False
    assert user["remote_privacy"] is False


def test_existing_privacy_user_is_preserved_during_migration(storage_module):
    store = make_store(storage_module)
    user = {"incognito": True, "remote_privacy": False}
    store._normalize_user(user)
    assert user["privacy_controls_enabled"] is True
    assert user["incognito"] is True


def test_disabling_privacy_controls_clears_both_privacy_modes(storage_module):
    store = make_store(storage_module)
    store._data = {
        "users": {
            "owner": {
                "privacy_controls_enabled": True,
                "incognito": True,
                "remote_privacy": True,
            }
        }
    }
    user = asyncio.run(
        store.async_set_preferences("owner", privacy_controls_enabled=False)
    )
    assert user["privacy_controls_enabled"] is False
    assert user["incognito"] is False
    assert user["remote_privacy"] is False


def test_cached_client_cannot_enable_privacy_while_controls_are_off(storage_module):
    store = make_store(storage_module)
    store._data = {"users": {"owner": {}}}
    user = asyncio.run(
        store.async_set_preferences(
            "owner",
            incognito=True,
            remote_privacy=True,
        )
    )
    assert user["privacy_controls_enabled"] is False
    assert user["incognito"] is False
    assert user["remote_privacy"] is False


def test_enabled_privacy_controls_allow_modes_to_be_selected(storage_module):
    store = make_store(storage_module)
    store._data = {"users": {"owner": {}}}
    user = asyncio.run(
        store.async_set_preferences(
            "owner",
            privacy_controls_enabled=True,
            incognito=True,
            remote_privacy=True,
        )
    )
    assert user["privacy_controls_enabled"] is True
    assert user["incognito"] is True
    assert user["remote_privacy"] is True


def test_panel_hides_quick_privacy_button_until_feature_is_enabled():
    panel = (COMPONENT / "www" / "investment-panel.js").read_text()
    assert 'id="privacy-controls-enabled"' in panel
    assert "savePrivacyControls" in panel
    assert "this._privacyControlsEnabled?" in panel
    assert 'id="incognito"' in panel
    assert "privacy_controls_enabled:next" in panel


def test_panel_reuses_opt_in_privacy_logic_on_every_portfolio_refresh():
    panel = (COMPONENT / "www" / "investment-panel.js").read_text()
    assert "this.applyPortfolioPayload(portfolio);" in panel
    legacy = "this._storedIncognito=!!portfolio?.incognito;"
    assert panel.count(legacy) == 0


def test_panel_starts_bootstrap_after_custom_element_is_connected():
    panel = (COMPONENT / "www" / "investment-panel.js").read_text()
    assert "first paint waits for the 61-second poll timer" in panel
    assert "if(this._haConnected&&!this._portfolio&&!this._loadingBootstrap)" in panel
    assert "if(!this._haConnected||!this.isConnected||this._loadingBootstrap)return;" in panel


def test_indications_websocket_accepts_privacy_control_preferences():
    websocket = (COMPONENT / "websocket.py").read_text()
    assert '"privacy_controls_enabled"' in websocket
    assert '"remote_privacy"' in websocket
    marker = 'preference_keys = {"base_currency", "language", "privacy_controls_enabled", "incognito", "remote_privacy"'
    assert marker in websocket
