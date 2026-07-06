"""Select entities for CloudEdge cameras."""
from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import CloudEdgeCoordinator
from .const import DOMAIN
from .stream_bridge import (
    STREAM_PROFILE_AUTO,
    STREAM_PROFILE_OPTIONS,
    normalize_stream_profile,
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up per-camera stream profile selectors."""
    coordinator: CloudEdgeCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    def _build_selects() -> list[SelectEntity]:
        return [
            CloudEdgeStreamProfileSelect(coordinator, serial_number, device_info)
            for serial_number, device_info in (coordinator.data or {}).items()
            if device_info.get("type_id") in [1, 2, 3, 4, 5]
        ]

    if not coordinator.data:
        @callback
        def _async_add_when_ready() -> None:
            if coordinator.data and not getattr(coordinator, "_selects_added", False):
                coordinator._selects_added = True
                async_add_entities(_build_selects())

        coordinator.async_add_listener(_async_add_when_ready)
        return

    async_add_entities(_build_selects())


class CloudEdgeStreamProfileSelect(
    CoordinatorEntity[CloudEdgeCoordinator], SelectEntity, RestoreEntity
):
    """Choose the native stream profile for one camera."""

    _attr_has_entity_name = True
    _attr_name = "Stream profile"
    _attr_icon = "mdi:video-switch"
    _attr_options = list(STREAM_PROFILE_OPTIONS)

    def __init__(
        self,
        coordinator: CloudEdgeCoordinator,
        serial_number: str,
        device_info: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._serial_number = serial_number
        self._device_info = device_info
        self._attr_unique_id = f"{DOMAIN}_{serial_number}_stream_profile"

    @property
    def device_info(self) -> dict[str, Any]:
        """Return the parent camera device information."""
        return {
            "identifiers": {(DOMAIN, self._serial_number)},
            "name": self._device_info.get("name", f"Camera {self._serial_number}"),
            "manufacturer": "CloudEdge",
            "model": self._device_info.get("type", "SmartEye Camera"),
            "serial_number": self._serial_number,
            "sw_version": self._device_info.get("firmware_version"),
        }

    @property
    def current_option(self) -> str:
        """Return the currently requested profile."""
        return self.coordinator.get_stream_profile(self._serial_number)

    async def async_added_to_hass(self) -> None:
        """Restore the per-camera selection across HA restarts."""
        await super().async_added_to_hass()
        previous = await self.async_get_last_state()
        try:
            profile = normalize_stream_profile(previous.state) if previous else STREAM_PROFILE_AUTO
        except ValueError:
            profile = STREAM_PROFILE_AUTO
        self.coordinator.set_stream_profile(self._serial_number, profile)

    async def async_select_option(self, option: str) -> None:
        """Apply a new stream profile."""
        self.coordinator.set_stream_profile(self._serial_number, option)
        self.async_write_ha_state()
