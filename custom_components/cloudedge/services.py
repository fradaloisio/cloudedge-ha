"""Services for CloudEdge integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Service names
SERVICE_SET_PARAMETER = "set_parameter"
SERVICE_GET_DEVICE_INFO = "get_device_info"
SERVICE_REFRESH_DEVICE = "refresh_device"
SERVICE_REFRESH_PARAMETERS = "refresh_parameters"
SERVICE_GET_COORDINATOR_INFO = "get_coordinator_info"
SERVICE_CLEAR_CACHE = "clear_cache"

# Service schemas. device_name and device_id are both optional at the
# schema level so either can be used as the target; the handlers enforce
# that at least one is provided.
SET_PARAMETER_SCHEMA = vol.Schema(
    {
        vol.Optional("device_name"): cv.string,
        vol.Optional("device_id"): cv.string,
        vol.Required("parameter_name"): cv.string,
        vol.Required("value"): vol.Any(int, float, str, bool),
    }
)

GET_DEVICE_INFO_SCHEMA = vol.Schema(
    {
        vol.Optional("device_name"): cv.string,
        vol.Optional("device_id"): cv.string,
        vol.Optional("include_config", default=True): cv.boolean,
    }
)

REFRESH_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("device_name"): cv.string,
        vol.Optional("device_id"): cv.string,
    }
)

REFRESH_PARAMETERS_SCHEMA = vol.Schema(
    {
        vol.Optional("device_name"): cv.string,
        vol.Optional("device_id"): cv.string,
    }
)

GET_COORDINATOR_INFO_SCHEMA = vol.Schema({})

CLEAR_CACHE_SCHEMA = vol.Schema({})  # No parameters needed


def _iter_coordinators(hass: HomeAssistant) -> list[Any]:
    """Return every loaded coordinator."""
    return [
        coord
        for coord in hass.data.get(DOMAIN, {}).values()
        if hasattr(coord, "client")
    ]


def _find_in_coordinator_data(
    coordinators: list[Any], *, device_id: str | None, device_name: str | None
) -> tuple[Any, dict[str, Any]] | None:
    """Resolve a device from already-fetched coordinator data.

    Local resolution needs no cloud round-trip and, when a device_id is
    supplied, is immune to duplicate device names.
    """
    if device_id is not None:
        for coord in coordinators:
            for info in (coord.data or {}).values():
                if str(info.get("device_id")) == str(device_id):
                    return coord, info
        return None
    if device_name is None:
        return None
    for coord in coordinators:
        for info in (coord.data or {}).values():
            if info.get("name") == device_name:
                return coord, info
    return None


async def _resolve_target(
    hass: HomeAssistant,
    call: ServiceCall,
) -> tuple[Any, dict[str, Any]]:
    """Return (coordinator, device_info) for the device targeted by a call."""
    device_name = call.data.get("device_name")
    device_id = call.data.get("device_id")
    if not device_name and not device_id:
        raise HomeAssistantError("Specify either device_name or device_id")

    coordinators = _iter_coordinators(hass)

    local = _find_in_coordinator_data(
        coordinators, device_id=device_id, device_name=device_name
    )
    if local is not None:
        return local

    if device_id is not None:
        raise HomeAssistantError(f"Device id {device_id} not found in any coordinator")

    # Legacy fallback: the device may exist in the cloud inventory but not
    # yet in coordinator data (e.g. added after the last refresh).
    for coord in coordinators:
        try:
            device = await hass.async_add_executor_job(
                coord.client.find_device_by_name, device_name
            )
        except Exception as err:
            _LOGGER.debug("Error finding device in coordinator: %s", err)
            continue
        if device:
            return coord, device

    raise HomeAssistantError(f"Device {device_name} not found in any coordinator")


def _read_configuration(client: Any, serial_number: str) -> dict:
    """Read and format a device's IoT configuration by serial number."""
    from cloudedge.iot_parameters import get_parameter_name, format_parameter_value

    try:
        config = client.get_device_config(serial_number)
    except Exception as err:
        _LOGGER.debug("Config read failed for %s: %s", serial_number, err)
        return {}

    iot_data = None
    if isinstance(config, dict):
        if "result" in config and "iot" in config["result"]:
            iot_data = config["result"]["iot"]
        elif "iot" in config:
            iot_data = config["iot"]
        elif any(key.isdigit() for key in config.keys()):
            iot_data = config
    if not isinstance(iot_data, dict):
        return {}

    return {
        code: {
            "name": get_parameter_name(code),
            "code": code,
            "value": value,
            "formatted": format_parameter_value(get_parameter_name(code), value),
        }
        for code, value in iot_data.items()
    }


def _read_device_info_by_identity(
    client: Any, device: dict[str, Any], include_config: bool
) -> dict[str, Any]:
    """Assemble device info from the exact identity, immune to duplicate names."""
    info = dict(device)
    try:
        status = client.get_device_status(device["device_id"])
        if status:
            info.update(status)
    except Exception as err:
        _LOGGER.debug("Status read failed for %s: %s", device.get("serial_number"), err)
    if include_config:
        info["configuration"] = _read_configuration(client, device["serial_number"])
    return info


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for CloudEdge integration.

    Services are domain-wide (shared by all config entries): register once.
    """
    if hass.services.has_service(DOMAIN, SERVICE_SET_PARAMETER):
        return

    async def async_set_parameter(call: ServiceCall) -> None:
        """Set a device parameter."""
        parameter_name = call.data["parameter_name"]
        value = call.data["value"]

        coordinator, device = await _resolve_target(hass, call)
        label = call.data.get("device_id") or call.data.get("device_name")

        serial_number = device.get("serial_number")
        if not serial_number:
            raise HomeAssistantError(f"Device {label} has no serial number")

        from cloudedge.iot_parameters import get_parameter_code_by_name

        parameter_code = get_parameter_code_by_name(parameter_name)
        if parameter_code is None:
            raise HomeAssistantError(
                f"Unknown parameter {parameter_name}; use an IoT parameter name "
                "such as FRONT_LIGHT_SWITCH or MOTION_DET_ENABLE"
            )
        # Encode boolean switch states as integers while preserving text
        # and fractional values for other IoT parameters.
        value = int(value) if isinstance(value, bool) else value
        _LOGGER.debug(
            "Setting parameter %s (%s) to %s for device %s",
            parameter_name,
            parameter_code,
            value,
            label,
        )
        # Route through the coordinator so the write targets the exact
        # device (serial number + device_id), even when several devices
        # share the same name. Pass the resolved identity: the fallback
        # device may not be in coordinator data yet.
        await coordinator.async_set_device_parameter(
            serial_number, parameter_code, value, device=device
        )
        await coordinator.async_request_refresh()

        _LOGGER.info(
            "Successfully set %s to %s for device %s",
            parameter_name,
            value,
            label,
        )

    async def async_get_device_info(call: ServiceCall) -> None:
        """Get device information."""
        include_config = call.data.get("include_config", True)

        coordinator, device = await _resolve_target(hass, call)
        label = call.data.get("device_id") or call.data.get("device_name")
        device_name = device.get("name", label)

        def _read() -> dict[str, Any] | None:
            try:
                info = coordinator.client.get_device_info(device_name, include_config)
            except Exception as err:
                _LOGGER.debug("get_device_info failed for %s: %s", label, err)
                info = None
            if info is None or (
                device.get("device_id") is not None
                and str(info.get("device_id")) != str(device.get("device_id"))
            ):
                # Duplicate names made the library resolve a different
                # device: re-read through the exact identity that
                # _resolve_target selected.
                info = _read_device_info_by_identity(
                    coordinator.client, device, include_config
                )
            return info

        device_info = await hass.async_add_executor_job(_read)

        if not device_info:
            raise HomeAssistantError(f"Failed to get device info for {label}")

        _LOGGER.info("Device info for %s: %s", device_name, device_info)
        # You could emit an event here with the device info
        hass.bus.async_fire(
            f"{DOMAIN}_device_info",
            {
                "device_name": device_name,
                "device_info": device_info,
            },
        )

    async def async_refresh_device(call: ServiceCall) -> None:
        """Refresh device data."""
        if not call.data.get("device_name") and not call.data.get("device_id"):
            # Refresh all coordinators
            _LOGGER.debug("Refreshing data for all devices")
            for coord in _iter_coordinators(hass):
                if hasattr(coord, "async_request_refresh"):
                    await coord.async_request_refresh()
            _LOGGER.info("Refreshed data for all devices")
            return

        coordinator, device = await _resolve_target(hass, call)
        await coordinator.async_request_refresh()
        _LOGGER.info("Refreshed data for device %s", device.get("name"))

    async def async_refresh_parameters(call: ServiceCall) -> None:
        """Refresh parameters for a specific device."""
        coordinator, device = await _resolve_target(hass, call)
        device_name = device.get("name")
        serial_number = device.get("serial_number")
        if not serial_number:
            raise HomeAssistantError(f"Device {device_name} has no serial number")

        # Use the coordinator's targeted refresh method
        success = await coordinator.async_refresh_device_config(
            device_name, serial_number=serial_number
        )

        if not success:
            raise HomeAssistantError(
                f"Failed to refresh parameters for device {device_name}"
            )
        _LOGGER.info("Successfully refreshed parameters for device %s", device_name)

    def _all_coordinators() -> list[Any]:
        """Return every loaded coordinator, not just the first one."""
        return [
            hass.data[DOMAIN][config_entry.entry_id]
            for config_entry in hass.config_entries.async_entries(DOMAIN)
            if config_entry.entry_id in hass.data.get(DOMAIN, {})
        ]

    async def async_get_coordinator_info(call: ServiceCall) -> None:
        """Get coordinator diagnostic information."""
        _LOGGER.info("Getting CloudEdge coordinator information...")

        coordinators = _all_coordinators()
        if not coordinators:
            raise HomeAssistantError("No CloudEdge coordinator found")

        for coordinator in coordinators:
            try:
                info = coordinator.get_coordinator_info()
                _LOGGER.info("CloudEdge Coordinator Info: %s", info)
            except Exception as e:
                raise HomeAssistantError(f"Error getting coordinator info: {e}") from e

    async def async_clear_cache(call: ServiceCall) -> None:
        """Clear CloudEdge session cache."""
        _LOGGER.info("Clearing CloudEdge session cache...")

        coordinators = [
            c for c in _all_coordinators() if hasattr(c, "cleanup_cache")
        ]
        if not coordinators:
            raise HomeAssistantError("No CloudEdge coordinator found")

        for coordinator in coordinators:
            # cleanup_cache does disk I/O — keep it off the event loop
            await hass.async_add_executor_job(coordinator.cleanup_cache)
        _LOGGER.info("CloudEdge session cache cleared for %d account(s)", len(coordinators))

    # Register services
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_PARAMETER,
        async_set_parameter,
        schema=SET_PARAMETER_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_DEVICE_INFO,
        async_get_device_info,
        schema=GET_DEVICE_INFO_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_DEVICE,
        async_refresh_device,
        schema=REFRESH_DEVICE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_PARAMETERS,
        async_refresh_parameters,
        schema=REFRESH_PARAMETERS_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_COORDINATOR_INFO,
        async_get_coordinator_info,
        schema=GET_COORDINATOR_INFO_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_CACHE,
        async_clear_cache,
        schema=CLEAR_CACHE_SCHEMA,
    )

    _LOGGER.info("CloudEdge services registered")


async def async_unload_services(hass: HomeAssistant) -> None:
    """Unload services."""
    hass.services.async_remove(DOMAIN, SERVICE_SET_PARAMETER)
    hass.services.async_remove(DOMAIN, SERVICE_GET_DEVICE_INFO)
    hass.services.async_remove(DOMAIN, SERVICE_REFRESH_DEVICE)
    hass.services.async_remove(DOMAIN, SERVICE_REFRESH_PARAMETERS)
    hass.services.async_remove(DOMAIN, SERVICE_GET_COORDINATOR_INFO)
    hass.services.async_remove(DOMAIN, SERVICE_CLEAR_CACHE)
    _LOGGER.info("CloudEdge services unloaded")
