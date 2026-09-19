from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "investment"


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def current_revision() -> str:
    match = re.search(r'^PANEL_ASSET_REVISION = "([^"]+)"$', text("const.py"), re.M)
    assert match is not None
    return match.group(1)


def test_panel_identity_has_one_canonical_revision_declaration():
    const = text("const.py")
    revision = current_revision()
    assert re.fullmatch(r"\d+\.\d+\.\d+-r\d+", revision)
    assert const.count("PANEL_ASSET_REVISION =") == 1
    assert 'PANEL_NAME = f"investment-panel-{PANEL_ASSET_REVISION.rsplit(\'-\', 1)[-1]}"' in const


def test_backend_passes_canonical_revision_to_runtime_module():
    init = text("__init__.py")
    assert "webcomponent_name=PANEL_NAME" in init
    assert 'f"?asset_revision={PANEL_ASSET_REVISION}&runtime_revision={runtime_revision}"' in init
    assert 'investment-panel-runtime.js?v=' not in init


def test_runtime_derives_panel_import_and_element_name_from_query_revision():
    runtime = text("www/investment-panel-runtime.js")
    assert 'const runtimeUrl = new URL(import.meta.url);' in runtime
    assert 'runtimeUrl.searchParams.get("asset_revision")' in runtime
    assert 'const PANEL_ELEMENT_SUFFIX = PANEL_ASSET_REVISION.split("-").at(-1);' in runtime
    assert 'const PANEL_ELEMENT_NAME = `investment-panel-${PANEL_ELEMENT_SUFFIX}`;' in runtime
    assert 'await import(`./investment-panel.js?v=${encodeURIComponent(PANEL_ASSET_REVISION)}`);' in runtime
    assert "customElements.get(PANEL_ELEMENT_NAME)" in runtime
    assert not re.search(r'investment-panel-r\d+', runtime)


def test_base_panel_derives_custom_element_from_its_module_revision():
    panel = text("www/investment-panel.js")
    assert 'new URL(import.meta.url).searchParams.get("v")' in panel
    assert 'const PANEL_MODULE_SUFFIX = PANEL_MODULE_REVISION.split("-").at(-1);' in panel
    assert 'const PANEL_MODULE_ELEMENT_NAME = `investment-panel-${PANEL_MODULE_SUFFIX}`;' in panel
    assert "customElements.get(PANEL_MODULE_ELEMENT_NAME)" in panel
    assert "customElements.define(PANEL_MODULE_ELEMENT_NAME,InvestmentPanel)" in panel
    assert not re.search(r'investment-panel-r\d+', panel)


def test_current_backend_name_matches_revision_suffix_without_hardcoding_revision():
    revision = current_revision()
    expected_name = f"investment-panel-{revision.rsplit('-', 1)[-1]}"
    assert expected_name.startswith("investment-panel-r")
    const = text("const.py")
    assert "PANEL_NAME = f" in const


def test_legacy_unversioned_component_cannot_capture_indications_panel():
    panel = text("www/investment-panel.js")
    runtime = text("www/investment-panel-runtime.js")
    assert 'customElements.define("investment-panel",InvestmentPanel)' not in panel
    assert 'customElements.get("investment-panel")' not in runtime
