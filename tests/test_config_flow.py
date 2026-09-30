"""Config flow regressions: validation outcomes and reauth targeting."""
import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

pytest.importorskip("homeassistant")

from custom_components.cloudedge.config_flow import (
    ConfigFlow,
    CannotConnect,
    InvalidAuth,
)
from custom_components.cloudedge.const import DOMAIN


USER_INPUT = {
    "username": "user@example.test",
    "password": "password",
    "country_code": "IT",
    "phone_code": "+39",
    "refresh_interval": 5,
}


def make_hass():
    hass = Mock()
    hass.config_entries = Mock()
    hass.config_entries.async_entries = Mock(return_value=[])
    hass.config_entries.async_get_entry = Mock(return_value=None)
    hass.config_entries.async_update_entry = Mock()
    hass.config_entries.async_reload = AsyncMock()
    hass.config_entries.flow = Mock()
    hass.config_entries.flow.async_progress_by_handler = Mock(return_value=[])
    hass.config_entries.flow.async_abort = Mock()
    hass.config_entries.async_entry_for_domain_unique_id = Mock(return_value=None)
    hass.config_entries.async_schedule_reload = Mock()
    return hass


def make_flow(hass, source="user"):
    flow = ConfigFlow()
    flow.hass = hass
    flow.context = {"source": source}
    return flow


def test_invalid_credentials_show_the_auth_error():
    async def run():
        flow = make_flow(make_hass())
        with patch(
            "custom_components.cloudedge.config_flow.validate_input",
            AsyncMock(side_effect=InvalidAuth("bad credentials")),
        ):
            result = await flow.async_step_user(dict(USER_INPUT))
        assert result["type"] == "form"
        assert result["errors"] == {"base": "invalid_auth"}

    asyncio.run(run())


def test_network_failure_shows_cannot_connect():
    async def run():
        flow = make_flow(make_hass())
        with patch(
            "custom_components.cloudedge.config_flow.validate_input",
            AsyncMock(side_effect=CannotConnect("unreachable")),
        ):
            result = await flow.async_step_user(dict(USER_INPUT))
        assert result["type"] == "form"
        assert result["errors"] == {"base": "cannot_connect"}

    asyncio.run(run())


def test_unexpected_failure_shows_unknown_error():
    async def run():
        flow = make_flow(make_hass())
        with patch(
            "custom_components.cloudedge.config_flow.validate_input",
            AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result = await flow.async_step_user(dict(USER_INPUT))
        assert result["errors"] == {"base": "unknown"}

    asyncio.run(run())


def test_successful_validation_creates_the_entry_with_the_username_title():
    async def run():
        flow = make_flow(make_hass())
        with patch(
            "custom_components.cloudedge.config_flow.validate_input",
            AsyncMock(return_value={"title": "CloudEdge (user@example.test)", "device_count": 2}),
        ):
            result = await flow.async_step_user(dict(USER_INPUT))
        assert result["type"] == "create_entry"
        assert result["title"] == "CloudEdge (user@example.test)"
        assert result["data"] == USER_INPUT

    asyncio.run(run())


def test_duplicate_account_aborts_before_validating():
    """A validation login kills the active vendor session (one session per
    account): it must not even start when the account is already set up."""
    async def run():
        from homeassistant.data_entry_flow import AbortFlow

        hass = make_hass()
        existing = Mock(
            entry_id="e1",
            domain=DOMAIN,
            unique_id="user@example.test",
            data={"username": "user@example.test"},
        )
        hass.config_entries.async_entries = Mock(return_value=[existing])
        # A real hass resolves the duplicate through this lookup; the flow
        # manager converts the AbortFlow into an abort result.
        hass.config_entries.async_entry_for_domain_unique_id = Mock(
            return_value=existing
        )
        flow = make_flow(hass)
        with patch(
            "custom_components.cloudedge.config_flow.validate_input",
            AsyncMock(),
        ) as validate:
            with pytest.raises(AbortFlow, match="already_configured"):
                await flow.async_step_user(dict(USER_INPUT))
        # The duplicate check aborted before any validation login, which
        # would have killed the active vendor session.
        validate.assert_not_awaited()

    asyncio.run(run())


def test_reauth_updates_only_the_entry_that_triggered_it():
    async def run():
        hass = make_hass()
        entry = Mock(entry_id="e1", data=dict(USER_INPUT))
        hass.config_entries.async_get_entry = Mock(return_value=entry)
        flow = make_flow(hass, source="reauth")
        flow.context = {"source": "reauth", "entry_id": "e1"}

        step = await flow.async_step_reauth(dict(USER_INPUT))
        assert step["type"] == "form"
        assert step["step_id"] == "reauth_confirm"

        with patch(
            "custom_components.cloudedge.config_flow.validate_input",
            AsyncMock(return_value={"title": "ok", "device_count": 1}),
        ):
            result = await flow.async_step_reauth_confirm({"password": "new-password"})

        assert result["type"] == "abort"
        assert result["reason"] == "reauth_successful"
        # Only the password changed; everything else came from the entry.
        updated = hass.config_entries.async_update_entry.call_args.kwargs["data"]
        assert updated["password"] == "new-password"
        assert updated["username"] == USER_INPUT["username"]
        hass.config_entries.async_reload.assert_awaited_once_with("e1")

    asyncio.run(run())


def test_reauth_with_bad_credentials_keeps_the_form_open():
    async def run():
        hass = make_hass()
        entry = Mock(entry_id="e1", data=dict(USER_INPUT))
        hass.config_entries.async_get_entry = Mock(return_value=entry)
        flow = make_flow(hass, source="reauth")
        flow.context = {"source": "reauth", "entry_id": "e1"}
        await flow.async_step_reauth(dict(USER_INPUT))

        with patch(
            "custom_components.cloudedge.config_flow.validate_input",
            AsyncMock(side_effect=InvalidAuth("nope")),
        ):
            result = await flow.async_step_reauth_confirm({"password": "wrong"})
        assert result["type"] == "form"
        assert result["errors"] == {"base": "invalid_auth"}
        hass.config_entries.async_update_entry.assert_not_called()

    asyncio.run(run())
