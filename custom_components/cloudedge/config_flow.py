"""Config flow for CloudEdge integration."""
from __future__ import annotations

import logging
import os
import tempfile
import uuid
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

    def _validate() -> int:
        """Authenticate and count devices (runs in executor).

        Uses a throwaway session cache path so the validation login's
        token is not persisted (default pycloudedge path would leave a
        token file in HA's working directory).
        """
        tmp_cache = os.path.join(
            tempfile.gettempdir(), f"cloudedge_validate_{uuid.uuid4().hex}"
        )
        try:
            client = CloudEdgeClient(
                username=username,
                password=password,
                country_code=country_code,
                phone_code=phone_code,
                debug=False,  # pycloudedge debug dumps raw API traffic (tokens) to logs
                session_cache_file=tmp_cache,
            )
            if not client.authenticate():
                raise InvalidAuth("Authentication failed")
            devices = client.get_all_devices()
            return len(devices) if devices else 0
        finally:
            try:
                os.remove(tmp_cache)
            except OSError:
                pass

    try:
        _LOGGER.debug("Validating CloudEdge credentials for %s", username)
        device_count = await hass.async_add_executor_job(_validate)

        _LOGGER.info(
            "Successfully validated CloudEdge credentials. Found %d devices.",
            device_count,
        )

        # Return info that will be stored in the config entry
        return {
            "title": f"CloudEdge ({username})",
            "device_count": device_count,
        }

    except InvalidAuth:
        raise
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
    _reauth_entry: config_entries.ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        
        if user_input is not None:
            # Duplicate check BEFORE logging in: a validation login kills
            # the active CloudEdge session (one session per account), so
            # don't pay that price for an account that is already set up.
            await self.async_set_unique_id(user_input[CONF_USERNAME].strip().lower())
            self._abort_if_unique_id_configured()

            try:
                info = await validate_input(self.hass, user_input)

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
        # Target the entry that triggered reauth, not a unique_id lookup
        # on user-typed input (a typo silently matched nothing before).
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle reauth confirmation step."""
        errors: dict[str, str] = {}
        entry = self._reauth_entry
        if entry is None:
            return self.async_abort(reason="unknown")

        if user_input is not None:
            # Only the password can change in reauth; everything else
            # comes from the existing entry.
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            try:
                await validate_input(self.hass, data)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                self.hass.config_entries.async_update_entry(entry, data=data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""