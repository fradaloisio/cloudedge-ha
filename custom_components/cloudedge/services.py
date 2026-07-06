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
SERVICE_DEBUG_API_STATUS = "debug_api_status"
SERVICE_GET_COORDINATOR_INFO = "get_coordinator_info"
SERVICE_CLEAR_CACHE = "clear_cache"

# Service schemas
SET_PARAMETER_SCHEMA = vol.Schema(
    {
        vol.Required("device_name"): cv.string,
        vol.Required("parameter_name"): cv.string,
        vol.Required("value"): vol.Any(int, float, str, bool),
    }
)

GET_DEVICE_INFO_SCHEMA = vol.Schema(
    {
        vol.Required("device_name"): cv.string,
        vol.Optional("include_config", default=True): cv.boolean,
    }
)

REFRESH_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("device_name"): cv.string,
    }
)

REFRESH_PARAMETERS_SCHEMA = vol.Schema(
    {
        vol.Required("device_name"): cv.string,
    }
)

GET_COORDINATOR_INFO_SCHEMA = vol.Schema({})

CLEAR_CACHE_SCHEMA = vol.Schema({})  # No parameters needed


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for CloudEdge integration.

    Services are domain-wide (shared by all config entries): register once.
    """
    if hass.services.has_service(DOMAIN, SERVICE_SET_PARAMETER):
        return

    async def async_set_parameter(call: ServiceCall) -> None:
        """Set a device parameter."""
        device_name = call.data["device_name"]
        parameter_name = call.data["parameter_name"]
        value = call.data["value"]

        _LOGGER.debug(
            "Setting parameter %s to %s for device %s",
            parameter_name,
            value,
            device_name,
        )

        # Find the coordinator for this device
        coordinator = None
        for entry_id, coord in hass.data[DOMAIN].items():
            if hasattr(coord, "client"):
                try:
                    device = await hass.async_add_executor_job(
                        coord.client.find_device_by_name, device_name
                    )
                    if device:
                        coordinator = coord
                        break
                except Exception as e:
                    _LOGGER.debug("Error finding device in coordinator %s: %s", entry_id, e)

        if not coordinator:
            raise HomeAssistantError(f"Device {device_name} not found in any coordinator")

        try:
            success = await hass.async_add_executor_job(
                coordinator.client.set_device_parameter,
                device_name,
                parameter_name,
                value,
            )
        except Exception as e:
            raise HomeAssistantError(
                f"Error setting parameter {parameter_name} for device {device_name}: {e}"
            ) from e

        if not success:
            raise HomeAssistantError(
                f"Failed to set {parameter_name} to {value} for device {device_name}"
            )

        _LOGGER.info(
            "Successfully set %s to %s for device %s",
            parameter_name,
            value,
            device_name,
        )
        # Refresh the coordinator to update entity states
        await coordinator.async_request_refresh()

    async def async_get_device_info(call: ServiceCall) -> None:
        """Get device information."""
        device_name = call.data["device_name"]
        include_config = call.data.get("include_config", True)

        _LOGGER.debug("Getting device info for %s", device_name)

        # Find the coordinator for this device
        coordinator = None
        for entry_id, coord in hass.data[DOMAIN].items():
            if hasattr(coord, "client"):
                try:
                    device = await hass.async_add_executor_job(
                        coord.client.find_device_by_name, device_name
                    )
                    if device:
                        coordinator = coord
                        break
                except Exception as e:
                    _LOGGER.debug("Error finding device in coordinator %s: %s", entry_id, e)

        if not coordinator:
            raise HomeAssistantError(f"Device {device_name} not found in any coordinator")

        try:
            device_info = await hass.async_add_executor_job(
                coordinator.client.get_device_info,
                device_name,
                include_config,
            )
        except Exception as e:
            raise HomeAssistantError(
                f"Error getting device info for {device_name}: {e}"
            ) from e

        if not device_info:
            raise HomeAssistantError(f"Failed to get device info for {device_name}")

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
        device_name = call.data.get("device_name")

        if device_name:
            _LOGGER.debug("Refreshing data for device %s", device_name)
            # Find the coordinator for this specific device
            coordinator = None
            for entry_id, coord in hass.data[DOMAIN].items():
                if hasattr(coord, "client"):
                    try:
                        device = await hass.async_add_executor_job(
                            coord.client.find_device_by_name, device_name
                        )
                        if device:
                            coordinator = coord
                            break
                    except Exception as e:
                        _LOGGER.debug("Error finding device in coordinator %s: %s", entry_id, e)

            if coordinator:
                await coordinator.async_request_refresh()
                _LOGGER.info("Refreshed data for device %s", device_name)
            else:
                raise HomeAssistantError(f"Device {device_name} not found")
        else:
            # Refresh all coordinators
            _LOGGER.debug("Refreshing data for all devices")
            for coord in hass.data[DOMAIN].values():
                if hasattr(coord, "async_request_refresh"):
                    await coord.async_request_refresh()
            _LOGGER.info("Refreshed data for all devices")

    async def async_refresh_parameters(call: ServiceCall) -> None:
        """Refresh parameters for a specific device."""
        device_name = call.data["device_name"]
        _LOGGER.debug("Refreshing parameters for device %s", device_name)
        
        # Find the coordinator for this device
        coordinator = None
        for entry_id, coord in hass.data[DOMAIN].items():
            if hasattr(coord, "client") and hasattr(coord, "async_refresh_device_config"):
                try:
                    # Check if this coordinator has the device
                    device = await hass.async_add_executor_job(
                        coord.client.find_device_by_name, device_name
                    )
                    if device:
                        coordinator = coord
                        break
                except Exception as e:
                    _LOGGER.debug("Error finding device in coordinator %s: %s", entry_id, e)

        if not coordinator:
            raise HomeAssistantError(f"Device {device_name} not found in any coordinator")

        # Use the coordinator's targeted refresh method
        success = await coordinator.async_refresh_device_config(device_name)

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
            if config_entry.entry_id in hass.data[DOMAIN]
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