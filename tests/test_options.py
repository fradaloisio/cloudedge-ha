"""Config entry options: defaults, the options form and their effect at setup."""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
import voluptuous as vol

pytest.importorskip("homeassistant")

from homeassistant.config_entries import OptionsFlowManager
from homeassistant.core import HomeAssistant

from custom_components.cloudedge import CloudEdgeCoordinator, async_setup_entry
from custom_components.cloudedge.config_flow import ConfigFlow
from custom_components.cloudedge.const import DOMAIN
from custom_components.cloudedge.options import entry_options, stream_options
from custom_components.cloudedge.stream_bridge import StreamOptions

SETUP_DATA = {
    "username": "user@example.test",
    "password": "password",
    "country_code": "IT",
    "phone_code": "+39",
}
CHOSEN = {
    "refresh_interval": 15,
    "battery_max_live_minutes": 3,
    "battery_keepalive": False,
    "stream_audio": False,
}


def make_entry(data=SETUP_DATA, options=None):
    entry = Mock(entry_id="test-entry")
    entry.data = dict(data)
    entry.options = dict(options or {})
    return entry


def open_options(entry):
    flow = ConfigFlow.async_get_options_flow(entry)
    flow.hass = Mock()
    flow.hass.config_entries.async_get_known_entry.return_value = entry
    flow.handler = entry.entry_id
    return flow


def test_entry_without_options_protects_battery_cameras_by_default():
    # Entries created before the options existed keep their setup interval.
    entry = make_entry({**SETUP_DATA, "refresh_interval": 7})

    assert entry_options(entry) == {
        "refresh_interval": 7,
        "battery_max_live_minutes": 10,
        "battery_keepalive": True,
        "stream_audio": True,
    }
    assert stream_options(entry) == StreamOptions(
        audio=True, battery_keepalive=True, battery_max_live=600.0
    )
    assert entry_options(make_entry())["refresh_interval"] == 5


def test_chosen_options_replace_the_defaults_and_the_setup_interval():
    entry = make_entry({**SETUP_DATA, "refresh_interval": 7}, CHOSEN)

    assert entry_options(entry) == CHOSEN
    assert stream_options(entry) == StreamOptions(
        audio=False, battery_keepalive=False, battery_max_live=180.0
    )


def test_options_form_shows_the_current_values_and_stores_the_new_ones():
    async def run():
        entry = make_entry({**SETUP_DATA, "refresh_interval": 7})

        form = await open_options(entry).async_step_init()

        assert (form["type"], form["step_id"]) == ("form", "init")
        shown = {
            str(field): field.description["suggested_value"]
            for field in form["data_schema"].schema
        }
        assert shown == entry_options(entry)

        saved = await open_options(entry).async_step_init(dict(CHOSEN))

        assert (saved["type"], saved["data"]) == ("create_entry", CHOSEN)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("stored", "submitted", "reloads"),
    [
        ({}, CHOSEN, 1),
        # An entry from before the options, saved as shown: nothing changes.
        ({}, None, 0),
        (CHOSEN, CHOSEN, 0),
    ],
    ids=["changed", "defaults-kept", "unchanged"],
)
def test_saving_the_options_reloads_the_entry_only_when_they_change(
    stored, submitted, reloads
):
    # Home Assistant's own options flow manager applies the result: a
    # changed option takes effect through a reload, which ends live views.
    async def run():
        entry = make_entry({**SETUP_DATA, "refresh_interval": 7}, stored)
        entry.update_listeners = []
        flow = open_options(entry)

        def update_entry(entry, *, options):
            changed, entry.options = options != entry.options, options
            return changed

        flow.hass.config_entries.async_update_entry = update_entry
        manager = OptionsFlowManager(flow.hass)
        form = await flow.async_step_init()
        shown = {
            str(field): field.description["suggested_value"]
            for field in form["data_schema"].schema
        }
        result = await flow.async_step_init(dict(submitted or shown))

        await manager.async_finish_flow(flow, result)

        assert flow.hass.config_entries.async_schedule_reload.call_count == reloads
        assert entry_options(entry) == (submitted or shown)

    asyncio.run(run())


@pytest.mark.parametrize(
    "invalid",
    [
        {"refresh_interval": 0},
        {"refresh_interval": 61},
        {"battery_max_live_minutes": -1},
        {"battery_max_live_minutes": 121},
    ],
    ids=lambda invalid: "-".join(f"{key}={value}" for key, value in invalid.items()),
)
def test_options_form_refuses_values_outside_their_range(invalid):
    async def run():
        form = await open_options(make_entry()).async_step_init()
        with pytest.raises(vol.Invalid):
            form["data_schema"]({**CHOSEN, **invalid})
        # 0 is the documented way to lift the live view limit.
        assert form["data_schema"]({**CHOSEN, "battery_max_live_minutes": 0})

    asyncio.run(run())


def test_setup_applies_the_options(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.data = {DOMAIN: {}}
        hass.config_entries = Mock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        entry = make_entry(options=CHOSEN)
        entry.async_on_unload = Mock()
        with patch("homeassistant.helpers.frame.report_usage"), \
             patch.object(CloudEdgeCoordinator, "async_validate_authentication", AsyncMock()), \
             patch.object(CloudEdgeCoordinator, "async_safe_first_refresh", AsyncMock()), \
             patch("custom_components.cloudedge.async_setup_services", AsyncMock()):
            assert await async_setup_entry(hass, entry)

        coordinator = hass.data[DOMAIN][entry.entry_id]
        assert coordinator.update_interval == timedelta(minutes=15)
        # Audio is off for every camera of the entry, streaming or not.
        assert coordinator.get_stream_diagnostics("sn")["stream_audio_codec"] is None
        await hass.async_stop(force=True)

    asyncio.run(run())
