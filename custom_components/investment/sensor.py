"""Optional Home Assistant sensor entities for HA Investment automations."""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import DOMAIN, EXPOSABLE_ENTITY_METRICS, SIGNAL_ENTITY_EXPOSURE_CHANGED

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=60)

_METRIC_SOURCE = {
    "portfolio_value": "total",
    "today_change": "today_change",
    "today_change_percent": "today_pct",
    "invested_principal": "asset_principal",
    "current_cost_basis": "all_in_cost",
    "other_costs": "other_cost_total",
    "asset_fees": "asset_fee_value",
    "total_pnl": "pnl",
    "total_pnl_percent": "pnl_pct",
    "holding_count": "holdings",
}
_MONETARY_METRICS = {
    "portfolio_value", "today_change", "invested_principal", "current_cost_basis",
    "other_costs", "asset_fees", "total_pnl",
}
_PERCENT_METRICS = {"today_change_percent", "total_pnl_percent"}


class InvestmentUserCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Refresh one opted-in user's portfolio once for all of their sensors."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, manager, user_id: str) -> None:
        self.manager = manager
        self.user_id = user_id
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"HA Investment entities {user_id}",
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        return await self.manager.async_portfolio(self.user_id)


class InvestmentPortfolioSensor(CoordinatorEntity[InvestmentUserCoordinator], SensorEntity):
    """One explicitly exposed per-user portfolio metric."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: InvestmentUserCoordinator,
        entry: ConfigEntry,
        user_id: str,
        user_name: str,
        metric: str,
    ) -> None:
        super().__init__(coordinator)
        self._user_id = user_id
        self._user_name = user_name
        self._metric = metric
        self._exposed = True
        self._attr_unique_id = f"{entry.entry_id}_{user_id}_{metric}"
        self._attr_translation_key = metric
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"user:{user_id}")},
            name=f"HA Investment · {user_name}",
            manufacturer="HA Investment",
            model="Private portfolio",
        )
        if metric in _MONETARY_METRICS:
            self._attr_device_class = SensorDeviceClass.MONETARY
        elif metric in _PERCENT_METRICS:
            self._attr_native_unit_of_measurement = "%"
        elif metric == "holding_count":
            self._attr_suggested_display_precision = 0

    @callback
    def set_exposed(self, exposed: bool) -> None:
        """Update whether this opt-in entity should currently publish a state."""
        changed = self._exposed != bool(exposed)
        self._exposed = bool(exposed)
        if changed and self.hass is not None:
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._exposed and super().available

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self._metric in _MONETARY_METRICS:
            return str((self.coordinator.data or {}).get("base_currency") or "EUR")
        if self._metric in _PERCENT_METRICS:
            return "%"
        return None

    @property
    def native_value(self) -> float | int | None:
        data = self.coordinator.data or {}
        source = _METRIC_SOURCE[self._metric]
        if self._metric == "holding_count":
            return len(data.get("holdings") or [])
        raw = data.get(source)
        if raw is None:
            return None
        value = float(raw)
        return round(value, 2) if self._metric in _MONETARY_METRICS else round(value, 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "portfolio_currency": data.get("base_currency"),
            "portfolio_updated_at": data.get("updated_at"),
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up only the automation sensors explicitly selected by portfolio users."""
    manager = hass.data[DOMAIN]["manager"]
    coordinators: dict[str, InvestmentUserCoordinator] = {}
    entities: dict[tuple[str, str], InvestmentPortfolioSensor] = {}

    async def _user_name(user_id: str) -> str:
        try:
            user = await hass.auth.async_get_user(user_id)
        except Exception:  # pragma: no cover - defensive against auth shutdown
            user = None
        return str(getattr(user, "name", None) or f"User {user_id[:8]}")

    async def _apply_exposure(user_id: str, selected_values) -> None:
        selected = {metric for metric in selected_values if metric in EXPOSABLE_ENTITY_METRICS}
        coordinator = coordinators.get(user_id)
        if selected and coordinator is None:
            coordinator = InvestmentUserCoordinator(hass, entry, manager, user_id)
            coordinators[user_id] = coordinator
            # Optional automation sensors must never make the private panel fail
            # to set up because one market provider is temporarily unavailable.
            await coordinator.async_refresh()

        new_entities: list[InvestmentPortfolioSensor] = []
        if coordinator is not None:
            user_name = await _user_name(user_id)
            for metric in EXPOSABLE_ENTITY_METRICS:
                key = (user_id, metric)
                entity = entities.get(key)
                if metric in selected and entity is None:
                    entity = InvestmentPortfolioSensor(coordinator, entry, user_id, user_name, metric)
                    entities[key] = entity
                    new_entities.append(entity)
                elif entity is not None:
                    entity.set_exposed(metric in selected)
            if selected and not new_entities:
                await coordinator.async_request_refresh()
        if new_entities:
            async_add_entities(new_entities, update_before_add=False)

    @callback
    def _exposure_changed(user_id: str, selected_values) -> None:
        entry.async_create_task(
            hass,
            _apply_exposure(user_id, selected_values),
            name=f"HA Investment expose entities {user_id}",
        )

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_ENTITY_EXPOSURE_CHANGED, _exposure_changed)
    )

    snapshot = await manager.async_entity_exposure_snapshot()
    for user_id, selected in snapshot.items():
        if selected:
            await _apply_exposure(user_id, selected)
