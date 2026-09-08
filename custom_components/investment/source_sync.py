"""Helpers for validating and atomically replacing the integration source tree."""
from __future__ import annotations

from collections.abc import Mapping
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from typing import Any
from zipfile import BadZipFile, ZipFile

DOMAIN = "investment"
_COMPONENT_MARKER = f"custom_components/{DOMAIN}/"
_REQUIRED_FILES = frozenset(
    {
        "__init__.py",
        "const.py",
        "manager.py",
        "manifest.json",
        "sensor.py",
        "websocket.py",
        "www/investment-panel.js",
    }
)


class SourceBundleError(ValueError):
    """Raised when a downloaded source bundle cannot be trusted for installation."""


def validated_workflow_succeeded(
    payload: Mapping[str, Any], sha: str, workflow_name: str = "Validate"
) -> bool:
    """Return True only when the newest matching workflow run completed successfully."""
    expected_sha = str(sha or "").strip().lower()
    runs = payload.get("workflow_runs")
    if not expected_sha or not isinstance(runs, list):
        return False

    for raw in runs:
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("head_sha") or "").strip().lower() != expected_sha:
            continue
        if str(raw.get("name") or "").strip() != workflow_name:
            continue
        return (
            str(raw.get("status") or "").strip().lower() == "completed"
            and str(raw.get("conclusion") or "").strip().lower() == "success"
        )
    return False


def _safe_relative_path(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SourceBundleError(f"Unsafe source path: {raw!r}")
    return path


def extract_component_files(payload: bytes) -> dict[str, bytes]:
    """Return validated integration files from a repository ZIP archive."""
    if not payload:
        raise SourceBundleError("Downloaded source archive is empty")

    files: dict[str, bytes] = {}
    try:
        with ZipFile(io.BytesIO(payload)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                normalized = info.filename.replace("\\", "/")
                marker_index = normalized.find(_COMPONENT_MARKER)
                if marker_index < 0:
                    continue
                relative = normalized[marker_index + len(_COMPONENT_MARKER) :]
                if not relative:
                    continue
                safe_path = _safe_relative_path(relative)
                key = safe_path.as_posix()
                if key in files:
                    raise SourceBundleError(f"Duplicate source path: {key}")
                files[key] = archive.read(info)
    except BadZipFile as err:
        raise SourceBundleError("Downloaded source archive is not a valid ZIP file") from err

    validate_component_files(files)
    return files


def validate_component_files(files: Mapping[str, bytes]) -> None:
    """Validate the minimum structure, manifest, and Python syntax."""
    missing = sorted(_REQUIRED_FILES.difference(files))
    if missing:
        raise SourceBundleError(f"Source archive is missing required files: {', '.join(missing)}")

    try:
        manifest = json.loads(files["manifest.json"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise SourceBundleError("Integration manifest is invalid") from err

    if manifest.get("domain") != DOMAIN:
        raise SourceBundleError("Source archive contains the wrong integration domain")

    for path, content in files.items():
        if not path.endswith(".py"):
            continue
        try:
            source = content.decode("utf-8")
            compile(source, f"<source>/{path}", "exec")
        except (UnicodeDecodeError, SyntaxError) as err:
            raise SourceBundleError(f"Python validation failed for {path}") from err


def local_matches_component(files: Mapping[str, bytes], component_dir: Path) -> bool:
    """Return True when every tracked source file matches the local installation."""
    for relative, expected in files.items():
        local_path = component_dir / relative
        try:
            if not local_path.is_file() or local_path.read_bytes() != expected:
                return False
        except OSError:
            return False
    return True


def _write_component_files(files: Mapping[str, bytes], destination: Path) -> None:
    for relative, content in files.items():
        target = destination / _safe_relative_path(relative).as_posix()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def install_component_files(files: Mapping[str, bytes], component_dir: Path) -> None:
    """Atomically replace the integration directory after validating the source tree."""
    validate_component_files(files)

    component_dir = component_dir.resolve()
    parent = component_dir.parent
    parent.mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix=f".{DOMAIN}-staging-", dir=parent))
    backup = parent / f".{DOMAIN}-previous"

    try:
        _write_component_files(files, staging)

        for relative, expected in files.items():
            staged = staging / relative
            if not staged.is_file() or staged.read_bytes() != expected:
                raise SourceBundleError(f"Staged source verification failed for {relative}")

        if backup.exists():
            shutil.rmtree(backup)

        had_existing = component_dir.exists()
        if had_existing:
            component_dir.rename(backup)

        try:
            staging.rename(component_dir)
        except BaseException:
            if component_dir.exists():
                shutil.rmtree(component_dir, ignore_errors=True)
            if had_existing and backup.exists():
                backup.rename(component_dir)
            raise

        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
