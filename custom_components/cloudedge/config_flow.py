"""Config flow for CloudEdge integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .const import (
    DOMAIN,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_COUNTRY_CODE,
    CONF_PHONE_CODE,
    CONF_REFRESH_INTERVAL,
    DEFAULT_REFRESH_INTERVAL,
    DEFAULT_COUNTRY_CODE,
    DEFAULT_PHONE_CODE,
    COUNTRY_CODES,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_COUNTRY_CODE, default=DEFAULT_COUNTRY_CODE): vol.In(
            list(COUNTRY_CODES.keys())
        ),
        vol.Optional(CONF_PHONE_CODE, default=DEFAULT_PHONE_CODE): vol.In(
            list(COUNTRY_CODES.values())
        ),
        vol.Optional(CONF_REFRESH_INTERVAL, default=DEFAULT_REFRESH_INTERVAL): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=60)
        ),
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """
    # Import here to avoid issues during startup
    from cloudedge import CloudEdgeClient
    from cloudedge.exceptions import AuthenticationError, CloudEdgeError

    username = data[CONF_USERNAME]
    password = data[CONF_PASSWORD]
    country_code = data[CONF_COUNTRY_CODE]
    phone_code = data[CONF_PHONE_CODE]

    try:
        # Create client and test authentication
        _LOGGER.debug("Creating CloudEdge client for %s", username)
        _LOGGER.debug("Country code: %s, Phone code: %s", country_code, phone_code)
        
        client = CloudEdgeClient(
            username=username,
            password=password,
            country_code=country_code,
            phone_code=phone_code,
            debug=False,  # pycloudedge debug dumps raw API traffic (tokens) to logs
        )

        # Test authentication
        _LOGGER.debug("Testing authentication...")
        success = await hass.async_add_executor_job(client.authenticate)
        if not success:
            _LOGGER.error("Authentication returned False")
            raise InvalidAuth("Authentication failed")
        _LOGGER.debug("Authentication successful")

        # Try to get devices to ensure we can fetch data
        _LOGGER.debug("Fetching devices...")
        try:
            devices = await hass.async_add_executor_job(client.get_all_devices)
            device_count = len(devices) if devices else 0
            _LOGGER.debug("Successfully retrieved %d devices", device_count)
        except Exception as device_error:
            _LOGGER.error("Error getting devices: %s", device_error, exc_info=True)
            raise

        _LOGGER.info(
            "Successfully validated CloudEdge credentials. Found %d devices.",
            device_count,
        )

        # Return info that will be stored in the config entry
        return {
            "title": f"CloudEdge ({username})",
            "device_count": device_count,
        }

    except AuthenticationError as e:
        _LOGGER.error("Authentication failed: %s", e)
        raise InvalidAuth("Authentication failed") from e
    except CloudEdgeError as e:
        _LOGGER.error("CloudEdge API error: %s", e, exc_info=True)
        raise CannotConnect("Cannot connect to CloudEdge API") from e
    except Exception as e:
        _LOGGER.error("Unexpected error during validation: %s", e, exc_info=True)
        raise CannotConnect("Unexpected error occurred") from e


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for CloudEdge."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
                
                # Check if already configured
                await self.async_set_unique_id(user_input[CONF_USERNAME])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(title=info["title"], data=user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
            description_placeholders={
                "refresh_interval_min": "1",
                "refresh_interval_max": "60",
            },
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Handle reauth flow."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle reauth confirmation step."""
        errors: dict[str, str] = {}
        
        if user_input is not None:
            try:
                await validate_input(self.hass, user_input)
                
                # Update the existing config entry
                existing_entry = await self.async_set_unique_id(user_input[CONF_USERNAME])
                if existing_entry:
                    self.hass.config_entries.async_update_entry(
                        existing_entry, data=user_input
                    )
                    await self.hass.config_entries.async_reload(existing_entry.entry_id)
                    return self.async_abort(reason="reauth_successful")
                    
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""