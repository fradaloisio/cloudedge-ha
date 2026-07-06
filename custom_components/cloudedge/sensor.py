"""Sensor platform for CloudEdge integration."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)

# Valid states for the connection status sensor
_CONNECTION_STATUS_OPTIONS = ["online", "dormancy", "offline", "unknown"]
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import CloudEdgeCoordinator
from .const import (
    DOMAIN,
    SENSOR_PARAMETERS,
    ENABLED_BY_DEFAULT_SENSOR_PARAMS,
)
from cloudedge.iot_parameters import (
    IOT_PARAMETERS,
    BOOLEAN_PARAMETERS,
    PERCENTAGE_PARAMETERS,
    format_parameter_value,
)

_LOGGER = logging.getLogger(__name__)

TIMESTAMP_PARAMETERS = {"LAST_CHECK_TIME", "DEVICE_ONLINE_TIME"}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up CloudEdge sensor platform."""
    coordinator: CloudEdgeCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    if not coordinator.data:
        _LOGGER.warning("No device data available yet")
        async_add_entities([])
        return

    sensors = []
    for serial_number, device_info in coordinator.data.items():
        # Connection status sensor is added for every device
        sensors.append(CloudEdgeConnectionStatusSensor(
            coordinator, serial_number, device_info
        ))

        if config := device_info.get("configuration"):
            device_name = device_info.get("name", serial_number)
            _LOGGER.info("Device %s has %d parameters", device_name, len(config))
            
            # Log which enabled-by-default parameters are present
            enabled_params_present = [code for code in ENABLED_BY_DEFAULT_SENSOR_PARAMS if code in config]
            _LOGGER.info("Device %s - Enabled-by-default params present: %s", 
                        device_name, enabled_params_present if enabled_params_present else "None")
            
            for param_name, param_key in SENSOR_PARAMETERS.items():
                if param_key in config:
                    enabled = param_key in ENABLED_BY_DEFAULT_SENSOR_PARAMS
                    _LOGGER.debug("Creating CloudEdgeConfigSensor %s (code %s) - enabled by default: %s", 
                                param_name, param_key, enabled)
                    sensors.append(CloudEdgeConfigSensor(
                        coordinator, serial_number, device_info, param_name, param_key
                    ))
            
            for param_code, param_info in config.items():
                if param_code not in SENSOR_PARAMETERS.values():
                    iot_param_info = IOT_PARAMETERS.get(param_code)
                    if iot_param_info:
                        param_name = iot_param_info["name"].lower()
                    else:
                        param_name = f"param_{param_code}"
                    enabled = param_code in ENABLED_BY_DEFAULT_SENSOR_PARAMS
                    if enabled:
                        _LOGGER.debug("Creating enabled-by-default sensor: %s (code %s)", param_name, param_code)
                    sensors.append(CloudEdgeGenericSensor(
                        coordinator, serial_number, device_info, param_name, param_code, param_info
                    ))
        else:
            sensors.append(CloudEdgeDeviceStatusSensor(
                coordinator, serial_number, device_info
            ))

    _LOGGER.info("Adding %d sensor entities", len(sensors))
    async_add_entities(sensors)


class CloudEdgeBaseSensor(CoordinatorEntity[CloudEdgeCoordinator], SensorEntity):
    """Base class for CloudEdge sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CloudEdgeCoordinator,
        serial_number: str,
        device_info: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
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
        """Return if sensor is available."""
        if not self.coordinator.last_update_success:
            return False
        device_data = self.coordinator.data.get(self._serial_number)
        return device_data is not None


class CloudEdgeConfigSensor(CloudEdgeBaseSensor):
    """Sensor for device configuration parameters."""

    def __init__(
        self,
        coordinator: CloudEdgeCoordinator,
        serial_number: str,
        device_info: dict[str, Any],
        param_name: str,
        param_key: str,
    ) -> None:
        """Initialize the config sensor."""
        super().__init__(coordinator, serial_number, device_info)
        self._param_name = param_name
        self._param_key = param_key
        self._attr_unique_id = f"{DOMAIN}_{serial_number}_{param_name}"
        self._attr_name = param_name.replace("_", " ").title()
        
        # Enable by default if in the enabled list
        self._attr_entity_registry_enabled_default = param_key in ENABLED_BY_DEFAULT_SENSOR_PARAMS
        
        if param_name == "battery_level":
            self._attr_device_class = SensorDeviceClass.BATTERY
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        elif param_name == "wifi_strength":
            # Don't use SIGNAL_STRENGTH device class since device reports percentage, not dB/dBm
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_icon = "mdi:wifi"
        elif param_name == "device_temperature":
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        elif param_name in ["motion_sensitivity", "speaker_volume", "microphone_volume"]:
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_entity_category = EntityCategory.CONFIG
        else:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> int | float | str | None:
        """Return the value of the sensor."""
        device_data = self.coordinator.data.get(self._serial_number)
        if not device_data:
            return None
        config = device_data.get("configuration", {})
        param_info = config.get(self._param_key)
        if not param_info:
            return None
        value = param_info.get("value")
        if self._param_name in ["battery_level", "wifi_strength", "motion_sensitivity", "speaker_volume", "microphone_volume"]:
            try:
                return int(value) if value is not None else None
            except (ValueError, TypeError):
                return None
        elif self._param_name == "device_temperature":
            try:
                return float(value) if value is not None else None
            except (ValueError, TypeError):
                return None
        return value

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
        }


class CloudEdgeGenericSensor(CloudEdgeBaseSensor):
    """Sensor for device parameter (disabled by default except for important ones)."""

    def __init__(
        self,
        coordinator: CloudEdgeCoordinator,
        serial_number: str,
        device_info: dict[str, Any],
        param_name: str,
        param_key: str,
        param_info: dict[str, Any],
    ) -> None:
        """Initialize the generic sensor."""
        super().__init__(coordinator, serial_number, device_info)
        self._param_name = param_name
        self._param_key = param_key
        self._param_info = param_info
        self._attr_unique_id = f"{DOMAIN}_{serial_number}_{param_name}"
        
        # Enable by default for important parameters
        self._attr_entity_registry_enabled_default = param_key in ENABLED_BY_DEFAULT_SENSOR_PARAMS
        
        iot_param_info = IOT_PARAMETERS.get(param_key)
        if iot_param_info:
            self._attr_name = iot_param_info["description"]
            self._iot_param_name = iot_param_info["name"]
        else:
            self._attr_name = f"Parameter {param_key}"
            self._iot_param_name = f"PARAM_{param_key}"
        
        # All generic sensors are diagnostic entities
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        
        if self._iot_param_name in PERCENTAGE_PARAMETERS:
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
            if "BATTERY" in self._iot_param_name:
                self._attr_device_class = SensorDeviceClass.BATTERY
        elif "TEMPERATURE" in self._iot_param_name:
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif "HUMIDITY" in self._iot_param_name:
            self._attr_device_class = SensorDeviceClass.HUMIDITY
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif self._iot_param_name in TIMESTAMP_PARAMETERS:
            self._attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def available(self) -> bool:
        """Return if sensor is available."""
        return self.coordinator.last_update_success and bool(self.coordinator.data)

    @property
    def native_value(self) -> int | float | str | datetime | None:
        """Return the value of the sensor."""
        device_data = self.coordinator.data.get(self._serial_number)
        if not device_data:
            return None
        config = device_data.get("configuration", {})
        param_info = config.get(self._param_key)
        if not param_info:
            return None
        value = param_info.get("value")
        
        if self._iot_param_name in TIMESTAMP_PARAMETERS:
            try:
                return datetime.fromtimestamp(int(value), tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                return None
        elif self._iot_param_name in BOOLEAN_PARAMETERS:
            try:
                return int(value) if value is not None else None
            except (ValueError, TypeError):
                return None
        elif self._iot_param_name in PERCENTAGE_PARAMETERS:
            try:
                return int(value) if value is not None else None
            except (ValueError, TypeError):
                return None
        elif "TEMPERATURE" in self._iot_param_name:
            try:
                return float(value) if value is not None else None
            except (ValueError, TypeError):
                return None
        else:
            if isinstance(value, (int, float)):
                return value
            elif isinstance(value, str):
                if len(str(value)) > 200:
                    return f"Text ({len(value)} chars)"
                try:
                    if '.' in value:
                        return float(value)
                    else:
                        return int(value)
                except (ValueError, TypeError):
                    return value
        return value

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
        formatted_value = format_parameter_value(self._iot_param_name, param_info.get("value"), debug_mode=False)
        return {
            "parameter_code": self._param_key,
            "raw_value": param_info.get("value"),
            "formatted_value": formatted_value,
            "parameter_name": self._iot_param_name,
        }


class CloudEdgeDeviceStatusSensor(CloudEdgeBaseSensor):
    """Status sensor for devices without configuration parameters."""

    def __init__(
        self,
        coordinator: CloudEdgeCoordinator,
        serial_number: str,
        device_info: dict[str, Any],
    ) -> None:
        """Initialize the status sensor."""
        super().__init__(coordinator, serial_number, device_info)
        self._attr_unique_id = f"{DOMAIN}_{serial_number}_status"
        self._attr_name = "Status"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> str:
        """Return the status of the device."""
        device_data = self.coordinator.data.get(self._serial_number)
        if not device_data:
            return "unavailable"
        return "online" if device_data.get("online", False) else "offline"

    @property
    def icon(self) -> str:
        """Return the icon."""
        device_data = self.coordinator.data.get(self._serial_number)
        if not device_data:
            return "mdi:help-circle"
        is_online = device_data.get("online", False)
        device_type = device_data.get("type", "").lower()
        if "chime" in device_type or "doorbell" in device_type:
            return "mdi:bell-ring" if is_online else "mdi:bell-off"
        elif "camera" in device_type:
            return "mdi:cctv" if is_online else "mdi:cctv-off"
        else:
            return "mdi:check-circle" if is_online else "mdi:close-circle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        device_data = self.coordinator.data.get(self._serial_number)
        if not device_data:
            return {}
        return {
            "serial_number": self._serial_number,
            "device_type": device_data.get("type"),
            "device_id": device_data.get("device_id"),
            "host_key": device_data.get("host_key"),
            "home_id": device_data.get("home_id"),
            "last_seen": device_data.get("last_seen"),
            "online": device_data.get("online", False),
        }


class CloudEdgeConnectionStatusSensor(CloudEdgeBaseSensor):
    """Sensor that exposes the three-state connection status of a camera.

    States:
      online   – camera is awake and reachable
      dormancy – battery camera is asleep (will wake on command)
      offline  – camera is unreachable
      unknown  – status could not be determined
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = _CONNECTION_STATUS_OPTIONS
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coordinator: CloudEdgeCoordinator,
        serial_number: str,
        device_info: dict[str, Any],
    ) -> None:
        """Initialize the connection status sensor."""
        super().__init__(coordinator, serial_number, device_info)
        self._attr_unique_id = f"{DOMAIN}_{serial_number}_connection_status"
        self._attr_name = "Connection Status"

    @property
    def native_value(self) -> str | None:
        """Return the current connection status."""
        device_data = self.coordinator.data.get(self._serial_number)
        if not device_data:
            return None
        if self.coordinator.is_device_streaming(self._serial_number):
            return "online"
        status = device_data.get("connection_status", "unknown")
        # Guard against unexpected values from the API
        return status if status in _CONNECTION_STATUS_OPTIONS else "unknown"

    @property
    def icon(self) -> str:
        """Return an icon that reflects the current connection state."""
        status = self.native_value
        if status == "online":
            return "mdi:wifi"
        if status == "dormancy":
            return "mdi:sleep"
        if status == "offline":
            return "mdi:wifi-off"
        return "mdi:help-circle-outline"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra diagnostic attributes."""
        device_data = self.coordinator.data.get(self._serial_number)
        if not device_data:
            return {}
        attrs: dict[str, Any] = {
            "serial_number": self._serial_number,
            "online_flag": device_data.get("online"),
            "device_type": device_data.get("type"),
            "stream_active": self.coordinator.is_device_streaming(self._serial_number),
        }
        mqtt_connected = getattr(
            self.coordinator, "_mqtt_listener", None
        )
        attrs["mqtt_connected"] = (
            mqtt_connected.connected if mqtt_connected else False
        )
        last_event = device_data.get("last_motion_event")
        if last_event:
            attrs["last_event"] = last_event
        last_time = device_data.get("last_motion_time")
        if last_time:
            from datetime import datetime, timezone
            attrs["last_event_time"] = datetime.fromtimestamp(
                last_time, tz=timezone.utc
            ).isoformat()
        return attrs
