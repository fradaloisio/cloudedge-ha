"""Switch platform for CloudEdge integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import CloudEdgeCoordinator
from .const import (
    DOMAIN,
    SWITCH_PARAMETERS,
    ENABLED_BY_DEFAULT_SWITCH_PARAMS,
)
from cloudedge.iot_parameters import (
    IOT_PARAMETERS,
    BOOLEAN_PARAMETERS,
    format_parameter_value,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up CloudEdge switch platform."""
    coordinator: CloudEdgeCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    registry = er.async_get(hass)
    added: set[str] = set()

    @callback
    def _async_add_missing() -> None:
        switches = []
        for serial_number, device_info in (coordinator.data or {}).items():
            config = dict(device_info.get("configuration") or {})
            for code, info in IOT_PARAMETERS.items():
                if info["name"] not in BOOLEAN_PARAMETERS:
                    continue
                legacy_id = f"{DOMAIN}_{serial_number}_{info['name'].lower()}_switch"
                curated = next((name for name, key in SWITCH_PARAMETERS.items() if key == code), None)
                if (registry.async_get_entity_id("switch", DOMAIN, legacy_id)
                        or (curated and registry.async_get_entity_id(
                            "switch", DOMAIN, f"{DOMAIN}_{serial_number}_{curated}"))):
                    config.setdefault(code, {})
            for param_name, param_key in SWITCH_PARAMETERS.items():
                if param_key not in config:
                    continue
                iot_info = IOT_PARAMETERS[param_key]
                legacy_id = f"{DOMAIN}_{serial_number}_{iot_info['name'].lower()}_switch"
                curated_id = f"{DOMAIN}_{serial_number}_{param_name}"
                legacy_exists = registry.async_get_entity_id("switch", DOMAIN, legacy_id)
                curated_exists = registry.async_get_entity_id("switch", DOMAIN, curated_id)
                # Before 1.4 these were generic switches. Keep their registered
                # identity (and user settings); do not force dashboard renames.
                # If both identities already exist, continue serving both.
                if legacy_exists:
                    switches.append(CloudEdgeGenericSwitch(
                        coordinator, serial_number, device_info,
                        iot_info["name"].lower(), param_key, config[param_key],
                    ))
                if not legacy_exists or curated_exists:
                    switches.append(CloudEdgeConfigSwitch(
                        coordinator, serial_number, device_info, param_name, param_key,
                    ))

            for param_code, param_info in config.items():
                if param_code in SWITCH_PARAMETERS.values():
                    continue
                iot_info = IOT_PARAMETERS.get(param_code)
                if iot_info and iot_info["name"] in BOOLEAN_PARAMETERS:
                    switches.append(CloudEdgeGenericSwitch(
                        coordinator, serial_number, device_info,
                        iot_info["name"].lower(), param_code, param_info,
                    ))

        new_entities = [entity for entity in switches if entity.unique_id not in added]
        if new_entities:
            added.update(entity.unique_id for entity in new_entities)
            async_add_entities(new_entities)

    config_entry.async_on_unload(coordinator.async_add_listener(_async_add_missing))
    _async_add_missing()


class CloudEdgeBaseSwitch(CoordinatorEntity[CloudEdgeCoordinator], SwitchEntity):
    """Base class for CloudEdge switches."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CloudEdgeCoordinator,
        serial_number: str,
        device_info: dict[str, Any],
    ) -> None:
        """Initialize the base switch."""
        super().__init__(coordinator)
        self._serial_number = serial_number
        self._device_info = device_info

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self._serial_number)},
            "name": self._device_info.get("name", f"Camera {self._serial_number}"),
            "manufacturer": "CloudEdge",
            "model": self._device_info.get("type", "SmartEye Camera"),
            "serial_number": self._serial_number,
            "sw_version": self._device_info.get("firmware_version"),
        }

    @property
    def available(self) -> bool:
        """Return if switch is available."""
        if not self.coordinator.last_update_success or not self.coordinator.data:
            return False
        device = self.coordinator.data.get(self._serial_number, {})
        return bool(device.get("configuration", {}).get(self._param_key))


class CloudEdgeConfigSwitch(CloudEdgeBaseSwitch):
    """Representation of a CloudEdge configuration switch."""

    def __init__(
        self,
        coordinator: CloudEdgeCoordinator,
        serial_number: str,
        device_info: dict[str, Any],
        switch_name: str,
        param_key: str,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, serial_number, device_info)
        self._switch_name = switch_name
        self._param_key = param_key
        self._attr_unique_id = f"{DOMAIN}_{serial_number}_{switch_name}"
        self._attr_name = switch_name.replace("_", " ").title()
        
        # All switches are configuration entities
        self._attr_entity_category = EntityCategory.CONFIG
        
        # Enable by default for specific switches (map code -> name first)
        iot_info = IOT_PARAMETERS.get(param_key)
        self._attr_entity_registry_enabled_default = bool(
            iot_info and iot_info["name"] in ENABLED_BY_DEFAULT_SWITCH_PARAMS
        )

    @property
    def is_on(self) -> bool | None:
        """Return true if switch is on."""
        device_data = self.coordinator.data.get(self._serial_number)
        if not device_data:
            return None

        config = device_data.get("configuration", {})
        param_info = config.get(self._param_key)
        
        if not param_info:
            return None

        value = param_info.get("value")
        
        # Convert value to boolean
        if value in [1, "1", True, "true", "True"]:
            return True
        elif value in [0, "0", False, "false", "False"]:
            return False
        else:
            return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._set_parameter("1")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._set_parameter("0")

    async def _set_parameter(self, value: str) -> None:
        """Set a parameter on this entity's device by its stable serial number."""
        await self.coordinator.async_set_device_parameter(
            self._serial_number, self._param_key, int(value)
        )

    @property
    def icon(self) -> str:
        """Return the icon for the switch."""
        if self._switch_name == "front_light":
            return "mdi:lightbulb"
        elif self._switch_name == "motion_detection":
            return "mdi:motion-sensor"
        elif self._switch_name == "led_enable":
            return "mdi:led-on"
        elif self._switch_name == "sound_detection":
            return "mdi:microphone"
        
        return "mdi:toggle-switch"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        device_data = self.coordinator.data.get(self._serial_number)
        if not device_data:
            return {}

        config = device_data.get("configuration", {})
        param_info = config.get(self._param_key)
        
        if not param_info:
            return {}

        return {
            "parameter_code": self._param_key,
            "raw_value": param_info.get("value"),
            "formatted_value": param_info.get("formatted"),
            "description": param_info.get("description"),
        }


class CloudEdgeGenericSwitch(CloudEdgeBaseSwitch):
    """Switch for any boolean device configuration parameter (disabled by default)."""

    def __init__(
        self,
        coordinator: CloudEdgeCoordinator,
        serial_number: str,
        device_info: dict[str, Any],
        param_name: str,
        param_key: str,
        param_info: dict[str, Any],
    ) -> None:
        """Initialize the generic switch."""
        super().__init__(coordinator, serial_number, device_info)
        self._param_name = param_name
        self._param_key = param_key
        self._param_info = param_info
        self._attr_unique_id = f"{DOMAIN}_{serial_number}_{param_name}_switch"
        
        # Get name and description from IoT parameters
        iot_param_info = IOT_PARAMETERS.get(param_key)
        if iot_param_info:
            self._attr_name = iot_param_info["description"]
            self._iot_param_name = iot_param_info["name"]
        else:
            self._attr_name = f"Parameter {param_key}"
            self._iot_param_name = f"PARAM_{param_key}"
        
        # Set entity category as config since these control device behavior
        self._attr_entity_category = EntityCategory.CONFIG
        
        # Enable by default for specific switches
        self._attr_entity_registry_enabled_default = (
            iot_param_info and iot_param_info["name"] in ENABLED_BY_DEFAULT_SWITCH_PARAMS
        ) if iot_param_info else False

    @property
    def is_on(self) -> bool | None:
        """Return true if switch is on."""
        device_data = self.coordinator.data.get(self._serial_number)
        if not device_data:
            return None

        config = device_data.get("configuration", {})
        param_info = config.get(self._param_key)
        
        if not param_info:
            return None

        value = param_info.get("value")
        
        # Convert value to boolean
        if value in [1, "1", True, "true", "True"]:
            return True
        elif value in [0, "0", False, "false", "False"]:
            return False
        else:
            return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._set_parameter("1")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._set_parameter("0")

    async def _set_parameter(self, value: str) -> None:
        """Set a parameter on this entity's device by its stable serial number."""
        await self.coordinator.async_set_device_parameter(
            self._serial_number, self._param_key, int(value)
        )

    @property
    def icon(self) -> str:
        """Return the icon for the switch."""
        # Use specific icons based on parameter type/name
        param_name_lower = self._param_name.lower()
        
        if "light" in param_name_lower:
            return "mdi:lightbulb"
        elif "led" in param_name_lower:
            return "mdi:led-on"
        elif "motion" in param_name_lower:
            return "mdi:motion-sensor"
        elif "sound" in param_name_lower or "audio" in param_name_lower:
            return "mdi:volume-high"
        elif "record" in param_name_lower:
            return "mdi:record"
        elif "enable" in param_name_lower:
            return "mdi:toggle-switch"
        elif "wifi" in param_name_lower:
            return "mdi:wifi"
        else:
            return "mdi:toggle-switch-outline"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        device_data = self.coordinator.data.get(self._serial_number)
        if not device_data:
            return {}

        config = device_data.get("configuration", {})
        param_info = config.get(self._param_key)
        
        if not param_info:
            return {}

        # Use IoT parameter formatting for display
        formatted_value = format_parameter_value(
            self._iot_param_name, 
            param_info.get("value"), 
            debug_mode=False
        )

        return {
            "parameter_code": self._param_key,
            "raw_value": param_info.get("value"),
            "formatted_value": formatted_value,
            "description": param_info.get("description"),
            "parameter_name": self._iot_param_name,
            "iot_description": IOT_PARAMETERS.get(self._param_key, {}).get("description", "Unknown parameter"),
        }
