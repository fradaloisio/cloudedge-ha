"""Switch platform for CloudEdge integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
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
    get_parameter_name,
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

    # Handle case where coordinator.data might be None
    if not coordinator.data:
        _LOGGER.warning("No device data available yet, switch entities will be added when data is available")
        
        # Add a listener to create entities when data becomes available.
        # Must be a sync @callback: async_add_listener invokes listeners
        # synchronously, so an async def would never be awaited.
        @callback
        def _handle_coordinator_update() -> None:
            if coordinator.data and not getattr(coordinator, '_switches_added', False):
                _LOGGER.info("Device data is now available, adding switch entities")
                switches = []
                for serial_number, device_info in coordinator.data.items():
                    # Add configuration-based switches
                    if config := device_info.get("configuration"):
                        # Add known switch parameters first
                        for param_name, param_key in SWITCH_PARAMETERS.items():
                            if param_key in config:
                                switch = CloudEdgeConfigSwitch(
                                    coordinator, serial_number, device_info, param_name, param_key
                                )
                                switches.append(switch)
                        
                        # Add ALL boolean IoT parameters as switches (disabled by default)
                        for param_code, param_info in config.items():
                            if param_code not in SWITCH_PARAMETERS.values():
                                # Get the parameter name from IoT parameters
                                iot_param_info = IOT_PARAMETERS.get(param_code)
                                if iot_param_info and iot_param_info["name"] in BOOLEAN_PARAMETERS:
                                    param_name = iot_param_info["name"].lower()
                                    
                                    switch = CloudEdgeGenericSwitch(
                                        coordinator, serial_number, device_info, param_name, param_code, param_info
                                    )
                                    switches.append(switch)
                
                if switches:
                    async_add_entities(switches)
                    coordinator._switches_added = True
        
        coordinator.async_add_listener(_handle_coordinator_update)
        async_add_entities([])
        return

    switches = []
    for serial_number, device_info in coordinator.data.items():
        # Add configuration-based switches
        if config := device_info.get("configuration"):
            device_name = device_info.get("name", serial_number)
            _LOGGER.debug("Device %s - Setting up switches", device_name)
            
            # Add known switch parameters first
            for param_name, param_key in SWITCH_PARAMETERS.items():
                if param_key in config:
                    iot_info = IOT_PARAMETERS.get(param_key)
                    enabled = bool(iot_info and iot_info["name"] in ENABLED_BY_DEFAULT_SWITCH_PARAMS)
                    _LOGGER.debug("Creating CloudEdgeConfigSwitch %s (%s) - enabled by default: %s",
                                param_name, param_key, enabled)
                    switch = CloudEdgeConfigSwitch(
                        coordinator, serial_number, device_info, param_name, param_key
                    )
                    switches.append(switch)
            
            # Add ALL boolean IoT parameters as switches (disabled by default)
            for param_code, param_info in config.items():
                if param_code not in SWITCH_PARAMETERS.values():
                    # Get the parameter name from IoT parameters
                    iot_param_info = IOT_PARAMETERS.get(param_code)
                    if iot_param_info and iot_param_info["name"] in BOOLEAN_PARAMETERS:
                        param_name = iot_param_info["name"].lower()
                        enabled = iot_param_info["name"] in ENABLED_BY_DEFAULT_SWITCH_PARAMS
                        if enabled:
                            _LOGGER.debug("Creating enabled-by-default switch: %s (code %s)", param_name, param_code)
                        
                        switch = CloudEdgeGenericSwitch(
                            coordinator, serial_number, device_info, param_name, param_code, param_info
                        )
                        switches.append(switch)

    async_add_entities(switches)


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
        # Always available if coordinator has data, don't check device online status
        return self.coordinator.last_update_success and bool(self.coordinator.data)


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
        """Set the parameter value."""
        try:
            # Use the coordinator's client to set the parameter
            client = self.coordinator.client
            if client:
                # Find device name for parameter setting (API requires device name, not serial)
                device_data = self.coordinator.data.get(self._serial_number)
                if device_data and device_data.get('name'):
                    device_name = device_data['name']
                    
                    # Convert parameter code to parameter name (API expects names, not codes)
                    from cloudedge.iot_parameters import get_parameter_name
                    param_name = get_parameter_name(self._param_key)
                    
                    # Use the client's set_device_parameter method
                    success = await self.hass.async_add_executor_job(
                        client.set_device_parameter,
                        device_name,
                        param_name,  # Use parameter name, not code
                        int(value)  # Convert to int for API
                    )
                    
                    if success:
                        _LOGGER.debug(
                            "Successfully set parameter %s (%s) to %s for device %s",
                            param_name,
                            self._param_key,
                            value,
                            device_name,
                        )
                        
                        # Trigger parameter refresh to get updated values for all parameters
                        try:
                            await self.hass.services.async_call(
                                "cloudedge",
                                "refresh_parameters",
                                {"device_name": device_name},
                                blocking=False,  # Don't block the switch operation
                            )
                            _LOGGER.debug("Triggered parameter refresh for device %s (serial: %s) after setting %s", 
                                        device_name, self._serial_number, param_name)
                        except Exception as refresh_err:
                            _LOGGER.warning(
                                "Failed to trigger parameter refresh for device %s: %s",
                                device_name,
                                refresh_err
                            )
                            # Fallback to device-specific refresh if available, otherwise full refresh
                            if hasattr(self.coordinator, 'async_refresh_device_config'):
                                _LOGGER.debug("Using device-specific refresh fallback for %s", device_name)
                                await self.coordinator.async_refresh_device_config(device_name)
                            else:
                                await self.coordinator.async_request_refresh()
                    else:
                        _LOGGER.error(
                            "Failed to set parameter %s to %s for device %s - API returned false",
                            param_name,
                            value,
                            device_name,
                        )
                else:
                    _LOGGER.error("Device name not found for serial %s", self._serial_number)
            else:
                _LOGGER.error("CloudEdge client not available")
        except Exception as err:
            _LOGGER.error(
                "Failed to set parameter %s to %s for device %s: %s",
                self._param_key,
                value,
                self._serial_number,
                err,
            )
            raise

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
        """Set the parameter value."""
        try:
            # Use the coordinator's client to set the parameter
            client = self.coordinator.client
            if client:
                # Find device name for parameter setting (API requires device name, not serial)
                device_data = self.coordinator.data.get(self._serial_number)
                if device_data and device_data.get('name'):
                    device_name = device_data['name']
                    
                    # Convert parameter code to parameter name (API expects names, not codes)
                    param_name = self._iot_param_name  # Use the IoT parameter name
                    
                    # Use the client's set_device_parameter method
                    success = await self.hass.async_add_executor_job(
                        client.set_device_parameter,
                        device_name,
                        param_name,  # Use parameter name, not code
                        int(value)  # Convert to int for API
                    )
                    
                    if success:
                        _LOGGER.debug(
                            "Successfully set parameter %s (%s) to %s for device %s",
                            param_name,
                            self._param_key,
                            value,
                            device_name,
                        )
                        
                        # Trigger parameter refresh to get updated values for all parameters
                        try:
                            await self.hass.services.async_call(
                                "cloudedge",
                                "refresh_parameters",
                                {"device_name": device_name},
                                blocking=False,  # Don't block the switch operation
                            )
                            _LOGGER.debug("Triggered parameter refresh for device %s (serial: %s) after setting %s", 
                                        device_name, self._serial_number, param_name)
                        except Exception as refresh_err:
                            _LOGGER.warning(
                                "Failed to trigger parameter refresh for device %s: %s",
                                device_name,
                                refresh_err
                            )
                            # Fallback to device-specific refresh if available, otherwise full refresh
                            if hasattr(self.coordinator, 'async_refresh_device_config'):
                                _LOGGER.debug("Using device-specific refresh fallback for %s", device_name)
                                await self.coordinator.async_refresh_device_config(device_name)
                            else:
                                await self.coordinator.async_request_refresh()
                    else:
                        _LOGGER.error(
                            "Failed to set parameter %s to %s for device %s - API returned false",
                            param_name,
                            value,
                            device_name,
                        )
                else:
                    _LOGGER.error("Device name not found for serial %s", self._serial_number)
            else:
                _LOGGER.error("CloudEdge client not available")
        except Exception as err:
            _LOGGER.error(
                "Failed to set parameter %s to %s for device %s: %s",
                self._param_key,
                value,
                self._serial_number,
                err,
            )
            raise

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