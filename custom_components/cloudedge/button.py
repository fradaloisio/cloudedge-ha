"""CloudEdge button platform.

Provides button entities for CloudEdge devices to trigger actions like parameter refresh.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .entity import CloudEdgeEntityMixin

if TYPE_CHECKING:
    from . import CloudEdgeCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up CloudEdge button entities from a config entry."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]

    def _build_buttons() -> list[ButtonEntity]:
        return [
            CloudEdgeRefreshButton(coordinator, device_sn, device_data)
            for device_sn, device_data in (coordinator.data or {}).items()
        ]

    if not coordinator.data:
        _LOGGER.warning("No device data available yet, button entities will be added when data is available")

    added: set[str] = set()

    @callback
    def _async_add_missing() -> None:
        buttons = [
            button for button in _build_buttons()
            if button.unique_id not in added
        ]
        if buttons:
            added.update(button.unique_id for button in buttons)
            async_add_entities(buttons)

    config_entry.async_on_unload(coordinator.async_add_listener(_async_add_missing))
    _async_add_missing()


class CloudEdgeRefreshButton(
    CloudEdgeEntityMixin, CoordinatorEntity, ButtonEntity
):
    """Button entity to refresh device parameters."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:refresh"

    def __init__(
        self,
        coordinator,
        device_sn: str,
        device_data: Dict[str, Any],
    ) -> None:
        """Initialize the refresh button."""
        super().__init__(coordinator)
        self._serial_number = device_sn
        self._device_info = device_data
        self._device_name = device_data.get("name", "Unknown Device")
        self._last_refresh = None

        self._attr_name = "Refresh Parameters"
        self._attr_unique_id = f"{device_sn}_refresh_parameters"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes."""
        attrs = {}

        if self._last_refresh:
            attrs["last_refresh"] = self._last_refresh.isoformat()

        # Add device info
        attrs["device_name"] = self._device_name
        attrs["device_serial"] = self._serial_number
        attrs["button_available"] = self.available

        # Add parameter count if available
        device_data = self.coordinator.data.get(self._serial_number, {})
        config = device_data.get("configuration", {})
        if config:
            attrs["parameter_count"] = len(config)
            attrs["parameters_loaded"] = True
        else:
            attrs["parameters_loaded"] = False

        return attrs

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        # Button should be available as long as we have coordinator data
        # Even if device appears offline, user should be able to try refreshing
        return (
            self.coordinator.last_update_success
            and self._serial_number in self.coordinator.data
        )

    async def async_press(self) -> None:
        """Handle the button press to refresh device parameters."""
        _LOGGER.info("Refreshing parameters for device: %s", self._device_name)

        try:
            # Update refresh timestamp
            self._last_refresh = datetime.now(timezone.utc)

            success = await self.coordinator.async_refresh_device_config(
                self._device_name, serial_number=self._serial_number
            )
            if not success:
                raise HomeAssistantError("Failed to refresh device parameters")

            _LOGGER.info(f"Successfully triggered parameter refresh for {self._device_name}")
            
            # Update the entity state to reflect the new attributes
            self.async_write_ha_state()
                
        except Exception as e:
            # Reset timestamp on error so user knows the refresh failed
            self._last_refresh = None
            self.async_write_ha_state()
            raise HomeAssistantError(
                f"Error refreshing parameters for {self._device_name}: {e}"
            ) from e