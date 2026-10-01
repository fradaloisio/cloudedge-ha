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
from .entity import CloudEdgeEntityMixin, is_camera_device
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
            if is_camera_device(device_info)
        ]

    added: set[str] = set()

    @callback
    def _async_add_missing() -> None:
        selects = [
            select for select in _build_selects()
            if select.unique_id not in added
        ]
        if selects:
            added.update(select.unique_id for select in selects)
            async_add_entities(selects)

    config_entry.async_on_unload(coordinator.async_add_listener(_async_add_missing))
    _async_add_missing()


class CloudEdgeStreamProfileSelect(
    CloudEdgeEntityMixin,
    CoordinatorEntity[CloudEdgeCoordinator],
    SelectEntity,
    RestoreEntity,
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
