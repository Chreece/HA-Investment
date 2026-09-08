"""Source update entity for the alternate integration line."""
from __future__ import annotations

from datetime import timedelta
import logging
from pathlib import Path
import re
from typing import Any, override

from aiohttp import ClientTimeout

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .source_sync import (
    SourceBundleError,
    extract_component_files,
    install_component_files,
    local_matches_component,
    validated_workflow_succeeded,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=30)

_SOURCE_REPOSITORY = "Chreece/HA-Investment"
_SOURCE_REF = "indications"
_HEAD_URL = f"https://api.github.com/repos/{_SOURCE_REPOSITORY}/branches/{_SOURCE_REF}"
_CHECKS_URL = f"https://api.github.com/repos/{_SOURCE_REPOSITORY}/actions/runs"
_ARCHIVE_URL = f"https://codeload.github.com/{_SOURCE_REPOSITORY}/zip/{{sha}}"
_REQUEST_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "HA-Investment-source-sync",
}
_TIMEOUT = ClientTimeout(total=30)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _valid_sha(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if _SHA_RE.fullmatch(text) else None


def _display_sha(value: str | None) -> str | None:
    return value[:12] if value else None


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the source update entity."""
    entity = HAInvestmentSourceUpdate(hass, config_entry)
    await entity.async_initialize()
    async_add_entities([entity])


class HAInvestmentSourceUpdate(UpdateEntity):
    """Track and install the current source revision."""

    _attr_has_entity_name = False
    _attr_name = "HA Investment Source Update"
    _attr_should_poll = True
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    _attr_title = "HA Investment"
    _attr_auto_update = False

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        self.hass = hass
        self._attr_unique_id = f"{config_entry.entry_id}_source_update"
        self._store: Store[dict[str, str]] = Store(
            hass,
            1,
            f"{DOMAIN}.source_update.{config_entry.entry_id}",
        )
        self._installed_sha: str | None = None
        self._latest_sha: str | None = None

    @override
    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """Commit identities are unordered; any mismatch is an available update."""
        return latest_version != installed_version

    async def async_initialize(self) -> None:
        """Load the installed revision and establish a verified bootstrap state."""
        stored = await self._store.async_load() or {}
        self._installed_sha = _valid_sha(stored.get("installed_sha"))

        try:
            head_sha = await self._async_fetch_head_sha()
            ready = await self._async_revision_ready(head_sha)
        except Exception as err:
            _LOGGER.warning("Source revision check failed during setup: %s", err)
            self._sync_versions()
            return

        self._latest_sha = head_sha if ready else self._installed_sha

        if self._installed_sha is None and ready:
            try:
                payload = await self._async_download_archive(head_sha)
                files = await self.hass.async_add_executor_job(
                    extract_component_files, payload
                )
                component_dir = Path(
                    self.hass.config.path("custom_components", DOMAIN)
                )
                matches = await self.hass.async_add_executor_job(
                    local_matches_component, files, component_dir
                )
                if matches:
                    self._installed_sha = head_sha
                    self._latest_sha = head_sha
                    await self._async_save_installed_sha(head_sha)
            except Exception as err:
                _LOGGER.warning("Source bootstrap verification failed: %s", err)

        self._sync_versions()

    @override
    async def async_update(self) -> None:
        """Refresh the latest validated source revision."""
        try:
            head_sha = await self._async_fetch_head_sha()
            ready = await self._async_revision_ready(head_sha)
        except Exception as err:
            _LOGGER.debug("Source revision check failed: %s", err)
            return

        self._latest_sha = head_sha if ready else self._installed_sha
        self._sync_versions()

    @override
    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Install the latest verified source revision."""
        del version, backup, kwargs

        self._attr_in_progress = True
        self._attr_update_percentage = 5
        self.async_write_ha_state()

        try:
            target_sha = await self._async_fetch_head_sha()
            if not await self._async_revision_ready(target_sha):
                raise HomeAssistantError("Source revision has not passed validation")

            self._latest_sha = target_sha
            self._attr_update_percentage = 15
            self.async_write_ha_state()

            payload = await self._async_download_archive(target_sha)
            self._attr_update_percentage = 45
            self.async_write_ha_state()

            files = await self.hass.async_add_executor_job(
                extract_component_files, payload
            )
            self._attr_update_percentage = 65
            self.async_write_ha_state()

            component_dir = Path(
                self.hass.config.path("custom_components", DOMAIN)
            )
            await self.hass.async_add_executor_job(
                install_component_files, files, component_dir
            )
            self._attr_update_percentage = 90
            self.async_write_ha_state()

            self._installed_sha = target_sha
            await self._async_save_installed_sha(target_sha)
            self._sync_versions()
            self._attr_update_percentage = 100
            self.async_write_ha_state()
        except (SourceBundleError, OSError, ValueError) as err:
            raise HomeAssistantError(f"Source update failed validation: {err}") from err
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(f"Source update failed: {err}") from err
        finally:
            self._attr_in_progress = False
            self._attr_update_percentage = None
            self.async_write_ha_state()

    async def _async_fetch_head_sha(self) -> str:
        session = async_get_clientsession(self.hass)
        async with session.get(
            _HEAD_URL,
            headers=_REQUEST_HEADERS,
            timeout=_TIMEOUT,
        ) as response:
            if response.status != 200:
                raise HomeAssistantError(
                    f"Source revision request returned HTTP {response.status}"
                )
            data = await response.json(content_type=None)

        sha = _valid_sha(((data.get("commit") or {}).get("sha")))
        if sha is None:
            raise HomeAssistantError("Source revision response did not contain a valid commit")
        return sha

    async def _async_revision_ready(self, sha: str) -> bool:
        """Return True only after the repository validation workflow succeeds."""
        session = async_get_clientsession(self.hass)
        async with session.get(
            _CHECKS_URL,
            params={
                "branch": _SOURCE_REF,
                "head_sha": sha,
                "event": "push",
                "per_page": 10,
            },
            headers=_REQUEST_HEADERS,
            timeout=_TIMEOUT,
        ) as response:
            if response.status != 200:
                raise HomeAssistantError(
                    f"Source validation request returned HTTP {response.status}"
                )
            data = await response.json(content_type=None)

        return validated_workflow_succeeded(data, sha)

    async def _async_download_archive(self, sha: str) -> bytes:
        if _valid_sha(sha) is None:
            raise HomeAssistantError("Invalid source revision")

        session = async_get_clientsession(self.hass)
        async with session.get(
            _ARCHIVE_URL.format(sha=sha),
            headers={"User-Agent": _REQUEST_HEADERS["User-Agent"]},
            timeout=_TIMEOUT,
        ) as response:
            if response.status != 200:
                raise HomeAssistantError(
                    f"Source archive request returned HTTP {response.status}"
                )
            payload = await response.read()

        if not payload:
            raise HomeAssistantError("Source archive was empty")
        return payload

    async def _async_save_installed_sha(self, sha: str) -> None:
        await self._store.async_save({"installed_sha": sha})

    def _sync_versions(self) -> None:
        self._attr_installed_version = (
            _display_sha(self._installed_sha) if self._installed_sha else "local"
        )
        self._attr_latest_version = _display_sha(self._latest_sha)
