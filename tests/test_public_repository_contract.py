"""Public repository presentation and HACS packaging contracts."""
from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
HACS = ROOT / "hacs.json"
MANIFEST = ROOT / "custom_components" / "investment" / "manifest.json"
BRAND = ROOT / "custom_components" / "investment" / "brand"

SHOWCASE = (
    "assets/banner.svg",
    "assets/ui-dashboard.svg",
    "assets/ui-search.svg",
    "assets/ui-transactions.svg",
    "assets/ui-costs.svg",
    "assets/ui-providers.svg",
    "assets/ui-analytics.svg",
    "assets/ui-history.svg",
    "assets/ui-mobile.svg",
)


def test_hacs_repository_contract_files_are_present_and_minimal():
    hacs = json.loads(HACS.read_text())
    assert hacs["name"] == "HA Investment"
    assert hacs["content_in_root"] is False
    assert isinstance(hacs.get("homeassistant"), str)
    assert (BRAND / "icon.png").is_file()

    manifest = json.loads(MANIFEST.read_text())
    for key in ("domain", "name", "version", "documentation", "issue_tracker", "codeowners"):
        assert manifest.get(key)
    assert manifest["domain"] == "investment"


def test_public_showcase_assets_exist_and_are_valid_svg():
    for relative in SHOWCASE:
        path = ROOT / relative
        assert path.is_file(), relative
        ET.fromstring(path.read_text())


def test_readme_references_every_showcase_asset_and_hacs_link():
    text = README.read_text()
    for relative in SHOWCASE:
        assert relative in text
    assert "my.home-assistant.io/redirect/hacs_repository/" in text
    assert "actions/workflows/validate.yml" in text


def test_public_readme_does_not_reference_private_feature_line():
    forbidden = "indi" + "cation"
    assert forbidden not in README.read_text().casefold()
