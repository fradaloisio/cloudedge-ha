""""Preload camera stream" on a battery camera is flagged as a repair."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.camera.const import DATA_CAMERA_PREFS, DATA_COMPONENT
from homeassistant.components.camera.prefs import CameraPreferences
from homeassistant.helpers import issue_registry as ir

from custom_components.cloudedge.camera import CloudEdgeCamera
from custom_components.cloudedge.const import DOMAIN
from custom_components.cloudedge.repairs import async_create_fix_flow

from conftest import load_empty_registries
from test_coordinator import make_coordinator

ENTITY_ID = "camera.terrace"
BATTERY = {"154": {"name": "BATTERY_PERCENT", "value": 80}}
MAINS = {"1007": {"name": "WIFI_STRENGTH", "value": 70}}


async def add_camera(tmp_path, configuration):
    coordinator = make_coordinator(tmp_path)
    hass = coordinator.hass
    await load_empty_registries(hass)
    await ir.async_load(hass)
    preferences = hass.data[DATA_CAMERA_PREFS] = CameraPreferences(hass)
    await preferences.async_load()
    coordinator.data = {"sn": {"name": "Terrace", "configuration": configuration}}
    camera = CloudEdgeCamera(coordinator, "sn", coordinator.data["sn"])
    camera.hass = hass
    camera.entity_id = ENTITY_ID
    camera.async_write_ha_state = Mock()  # the entity is on no platform here
    return hass, coordinator, camera, preferences


def issues(hass):
    return {
        issue_id: issue
        for (domain, issue_id), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN
    }


def test_preload_on_a_battery_camera_raises_a_repair_until_it_is_turned_off(tmp_path):
    async def run():
        hass, coordinator, camera, preferences = await add_camera(tmp_path, BATTERY)
        await preferences.async_update(ENTITY_ID, preload_stream=True)

        await camera.async_added_to_hass()

        (issue,) = issues(hass).values()
        assert issue.translation_key == "preload_battery_camera"
        assert issue.translation_placeholders == {"camera": "Terrace"}
        assert issue.is_fixable

        # The user turns the preload off in the camera's settings: Home
        # Assistant announces nothing, the next update notices.
        await preferences.async_update(ENTITY_ID, preload_stream=False)
        coordinator.async_update_listeners()
        assert issues(hass) == {}

        # ...and on again, while Home Assistant is running.
        await preferences.async_update(ENTITY_ID, preload_stream=True)
        coordinator.async_update_listeners()
        assert len(issues(hass)) == 1

        await camera.async_will_remove_from_hass()
        assert issues(hass) == {}
        await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("configuration", "preload"),
    [(MAINS, True), (BATTERY, False)],
    ids=["mains-camera", "preload-off"],
)
def test_no_repair_without_both_a_battery_and_the_preload(tmp_path, configuration, preload):
    async def run():
        hass, coordinator, camera, preferences = await add_camera(tmp_path, configuration)
        await preferences.async_update(ENTITY_ID, preload_stream=preload)

        await camera.async_added_to_hass()
        coordinator.async_update_listeners()

        assert issues(hass) == {}
        await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("watched", [False, True], ids=["nobody-watching", "someone-watching"])
def test_fixing_the_repair_turns_the_preload_off(tmp_path, watched):
    async def run():
        hass, coordinator, camera, preferences = await add_camera(tmp_path, BATTERY)
        await preferences.async_update(ENTITY_ID, preload_stream=True)
        await camera.async_added_to_hass()
        (issue_id, issue), = issues(hass).items()
        # The preloaded stream Home Assistant started for the camera.
        stream = camera.stream = Mock()
        stream.outputs.return_value = {"hls": Mock(idle=not watched)}
        stream.stop = AsyncMock()
        hass.data[DATA_COMPONENT] = Mock()
        hass.data[DATA_COMPONENT].get_entity.return_value = camera

        flow = await async_create_fix_flow(hass, issue_id, issue.data)
        flow.hass = hass
        form = await flow.async_step_init()
        assert (form["type"], form["step_id"]) == ("form", "confirm")
        assert form["description_placeholders"] == {"camera": "Terrace"}
        done = await flow.async_step_confirm({})

        assert done["type"] == "create_entry"
        settings = await preferences.get_dynamic_stream_settings(ENTITY_ID)
        assert settings.preload_stream is False
        # The stream nobody watches ends now, so the camera can sleep; a
        # view in progress is left alone and ends when its viewer leaves.
        assert stream.stop.await_count == (0 if watched else 1)
        await hass.async_stop(force=True)

    asyncio.run(run())
