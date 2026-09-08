from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
import json
from pathlib import Path
import sys
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "investment"


def load_module(name, filename):
    spec = spec_from_file_location(name, ROOT / filename)
    module = module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


source_sync = load_module("investment_source_sync_test", "source_sync.py")


def make_bundle(files):
    payload = BytesIO()
    with ZipFile(payload, "w") as archive:
        for relative, content in files.items():
            archive.writestr(
                f"HA-Investment-test/custom_components/investment/{relative}",
                content,
            )
    return payload.getvalue()


def valid_files():
    manifest = json.dumps({"domain": "investment"}).encode()
    return {
        "__init__.py": b"x = 1\n",
        "const.py": b"x = 1\n",
        "manager.py": b"x = 1\n",
        "manifest.json": manifest,
        "sensor.py": b"x = 1\n",
        "websocket.py": b"x = 1\n",
        "www/investment-panel.js": b"console.log('ok');\n",
    }


def test_extract_valid_bundle_and_match_local_tree(tmp_path):
    files = valid_files()
    extracted = source_sync.extract_component_files(make_bundle(files))
    assert extracted == files

    for relative, content in files.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    assert source_sync.local_matches_component(extracted, tmp_path)
    (tmp_path / "manager.py").write_text("changed\n", encoding="utf-8")
    assert not source_sync.local_matches_component(extracted, tmp_path)


def test_bundle_validation_rejects_wrong_domain():
    files = valid_files()
    files["manifest.json"] = json.dumps({"domain": "other"}).encode()
    try:
        source_sync.extract_component_files(make_bundle(files))
    except source_sync.SourceBundleError as err:
        assert "wrong integration domain" in str(err)
    else:
        raise AssertionError("wrong-domain source bundle was accepted")


def test_bundle_validation_rejects_invalid_python():
    files = valid_files()
    files["manager.py"] = b"def broken(:\n"
    try:
        source_sync.extract_component_files(make_bundle(files))
    except source_sync.SourceBundleError as err:
        assert "manager.py" in str(err)
    else:
        raise AssertionError("invalid Python source bundle was accepted")


def test_atomic_install_replaces_tree_and_removes_stale_files(tmp_path):
    component = tmp_path / "investment"
    component.mkdir()
    (component / "stale.txt").write_text("old", encoding="utf-8")

    files = valid_files()
    source_sync.install_component_files(files, component)

    assert not (component / "stale.txt").exists()
    assert (component / "manifest.json").read_bytes() == files["manifest.json"]
    assert source_sync.local_matches_component(files, component)


def test_validation_gate_requires_completed_success_for_exact_revision():
    sha = "a" * 40
    success = {
        "workflow_runs": [
            {
                "name": "Validate",
                "head_sha": sha,
                "status": "completed",
                "conclusion": "success",
            }
        ]
    }
    assert source_sync.validated_workflow_succeeded(success, sha)

    pending = {
        "workflow_runs": [
            {
                "name": "Validate",
                "head_sha": sha,
                "status": "in_progress",
                "conclusion": None,
            }
        ]
    }
    failed = {
        "workflow_runs": [
            {
                "name": "Validate",
                "head_sha": sha,
                "status": "completed",
                "conclusion": "failure",
            }
        ]
    }
    wrong_revision = {
        "workflow_runs": [
            {
                "name": "Validate",
                "head_sha": "b" * 40,
                "status": "completed",
                "conclusion": "success",
            }
        ]
    }
    assert not source_sync.validated_workflow_succeeded(pending, sha)
    assert not source_sync.validated_workflow_succeeded(failed, sha)
    assert not source_sync.validated_workflow_succeeded(wrong_revision, sha)


def test_validation_gate_uses_newest_matching_run():
    sha = "c" * 40
    payload = {
        "workflow_runs": [
            {
                "name": "Validate",
                "head_sha": sha,
                "status": "in_progress",
                "conclusion": None,
            },
            {
                "name": "Validate",
                "head_sha": sha,
                "status": "completed",
                "conclusion": "success",
            },
        ]
    }
    assert not source_sync.validated_workflow_succeeded(payload, sha)


def test_integration_forwards_source_update_platform():
    init_text = (ROOT / "__init__.py").read_text(encoding="utf-8")
    assert '_PLATFORMS = ["sensor", "update"]' in init_text
    assert "async_forward_entry_setups(entry, _PLATFORMS)" in init_text
    assert "async_unload_platforms(entry, _PLATFORMS)" in init_text

    update_text = (ROOT / "update.py").read_text(encoding="utf-8")
    assert "validated_workflow_succeeded" in update_text
    assert "Source revision has not passed validation" in update_text
