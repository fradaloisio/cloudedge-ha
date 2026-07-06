"""Services for CloudEdge integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
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
            _LOGGER.error("Device %s not found in any coordinator", device_name)
            return

        try:
            success = await hass.async_add_executor_job(
                coordinator.client.set_device_parameter,
                device_name,
                parameter_name,
                value,
            )

            if success:
                _LOGGER.info(
                    "Successfully set %s to %s for device %s",
                    parameter_name,
                    value,
                    device_name,
                )
                # Refresh the coordinator to update entity states
                await coordinator.async_request_refresh()
            else:
                _LOGGER.error(
                    "Failed to set %s to %s for device %s",
                    parameter_name,
                    value,
                    device_name,
                )

        except Exception as e:
            _LOGGER.error(
                "Error setting parameter %s for device %s: %s",
                parameter_name,
                device_name,
                e,
            )

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
            _LOGGER.error("Device %s not found in any coordinator", device_name)
            return

        try:
            device_info = await hass.async_add_executor_job(
                coordinator.client.get_device_info,
                device_name,
                include_config,
            )

            if device_info:
                _LOGGER.info("Device info for %s: %s", device_name, device_info)
                # You could emit an event here with the device info
                hass.bus.async_fire(
                    f"{DOMAIN}_device_info",
                    {
                        "device_name": device_name,
                        "device_info": device_info,
                    },
                )
            else:
                _LOGGER.error("Failed to get device info for %s", device_name)

        except Exception as e:
            _LOGGER.error("Error getting device info for %s: %s", device_name, e)

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
                _LOGGER.error("Device %s not found", device_name)
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
            _LOGGER.error("Device %s not found in any coordinator", device_name)
            return

        # Use the coordinator's targeted refresh method
        success = await coordinator.async_refresh_device_config(device_name)
        
        if success:
            _LOGGER.info("Successfully refreshed parameters for device %s", device_name)
        else:
            _LOGGER.warning("Failed to refresh parameters for device %s", device_name)

    

    async def async_get_coordinator_info(call: ServiceCall) -> None:
        """Get coordinator diagnostic information."""
        _LOGGER.info("Getting CloudEdge coordinator information...")
        
        # Find any coordinator
        coordinator = None
        for config_entry in hass.config_entries.async_entries(DOMAIN):
            if config_entry.entry_id in hass.data[DOMAIN]:
                coordinator = hass.data[DOMAIN][config_entry.entry_id]
                break
        
        if not coordinator:
            _LOGGER.error("No CloudEdge coordinator found")
            return
            
        try:
            info = coordinator.get_coordinator_info()
            _LOGGER.info("CloudEdge Coordinator Info: %s", info)
        except Exception as e:
            _LOGGER.error("Error getting coordinator info: %s", e)
    
    async def async_clear_cache(call: ServiceCall) -> None:
        """Clear CloudEdge session cache."""
        _LOGGER.info("Clearing CloudEdge session cache...")
        
        # Find any coordinator and clear its cache
        coordinator = None
        for config_entry in hass.config_entries.async_entries(DOMAIN):
            if config_entry.entry_id in hass.data[DOMAIN]:
                coordinator = hass.data[DOMAIN][config_entry.entry_id]
                break
        
        if coordinator and hasattr(coordinator, 'cleanup_cache'):
            coordinator.cleanup_cache()
            _LOGGER.info("CloudEdge session cache cleared successfully")
        else:
            _LOGGER.warning("No CloudEdge coordinator found or cache cleanup method not available")

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