""""Preload camera stream" on a battery camera is flagged as a repair."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.camera.const import DATA_CAMERA_PREFS, DATA_COMPONENT
from homeassistant.components.camera.prefs import CameraPreferences
from homeassistant.components.stream import Stream
from homeassistant.components.stream import core as stream_core
from homeassistant.components.stream.core import STREAM_SETTINGS_NON_LL_HLS
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.cloudedge import async_remove_entry
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


@pytest.mark.parametrize("state", ["never-streamed", "streaming", "idle"])
def test_fixing_the_repair_turns_the_preload_off_and_ends_the_unwatched_stream(
    tmp_path, monkeypatch, state
):
    async def run():
        hass, coordinator, camera, preferences = await add_camera(tmp_path, BATTERY)
        await preferences.async_update(ENTITY_ID, preload_stream=True)
        await camera.async_added_to_hass()
        (issue_id, issue), = issues(hass).items()
        timers = []  # idle timeouts, fired by hand below
        monkeypatch.setattr(
            stream_core, "async_call_later",
            lambda hass, delay, action: timers.append(action) or (lambda: None),
        )
        # What Home Assistant's preload sets up at startup, worker aside.
        stream = camera.stream = Stream(
            hass, "http://127.0.0.1:9/stream.ts", {}, STREAM_SETTINGS_NON_LL_HLS,
            await preferences.get_dynamic_stream_settings(ENTITY_ID),
        )
        output = stream.add_provider("hls")
        if state != "never-streamed":
            output.idle_timer.start()  # what the first video segment does
        if state == "idle":
            timers.pop()(None)  # the timeout passed while the preload held on
            await hass.async_block_till_done()
            assert output.idle and stream.outputs()
        stream.stop = AsyncMock(wraps=stream.stop)
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
        if state != "idle":
            # A viewer would keep the stream; nobody watches, so it ends
            # when the idle timeout passes. Before the fix, a stream that
            # never got video had no timeout and ran on.
            assert stream.stop.await_count == 0
            timers.pop()(None)
            await hass.async_block_till_done()
        assert stream.stop.await_count == 1
        assert not stream.outputs()
        await hass.async_stop(force=True)

    asyncio.run(run())


def test_an_ignored_repair_stays_ignored_across_a_reload(tmp_path):
    # Saving options, a reauthentication or enabling a sensor reloads the
    # config entry: the camera entity is removed and added again (review).
    async def run():
        hass, coordinator, camera, preferences = await add_camera(tmp_path, BATTERY)
        registry = er.async_get(hass)
        registry.async_get_or_create(
            "camera", DOMAIN, "cloudedge_sn_camera", suggested_object_id="terrace"
        )
        coordinator.config_entry.disabled_by = None
        await preferences.async_update(ENTITY_ID, preload_stream=True)
        await camera.async_added_to_hass()
        (issue_id,) = issues(hass)
        ir.async_ignore_issue(hass, DOMAIN, issue_id, True)

        await camera.async_will_remove_from_hass()
        reloaded = CloudEdgeCamera(coordinator, "sn", coordinator.data["sn"])
        reloaded.hass, reloaded.entity_id = hass, ENTITY_ID
        reloaded.async_write_ha_state = Mock()
        await reloaded.async_added_to_hass()

        assert issues(hass)[issue_id].dismissed_version is not None

        # Deleted for good, the camera takes its repair along.
        registry.async_remove(ENTITY_ID)
        await reloaded.async_will_remove_from_hass()
        assert issues(hass) == {}
        await hass.async_stop(force=True)

    asyncio.run(run())


def test_removing_the_config_entry_removes_its_repairs(tmp_path):
    async def run():
        hass, coordinator, camera, preferences = await add_camera(tmp_path, BATTERY)
        er.async_get(hass).async_get_or_create(
            "camera", DOMAIN, "cloudedge_sn_camera", suggested_object_id="terrace"
        )
        coordinator.config_entry.disabled_by = None
        await preferences.async_update(ENTITY_ID, preload_stream=True)
        await camera.async_added_to_hass()
        ir.async_create_issue(  # another account's camera keeps its own
            hass, DOMAIN, "preload_battery_camera_other", is_fixable=True,
            severity=ir.IssueSeverity.WARNING, translation_key="preload_battery_camera",
            data={"entity_id": "camera.other", "camera": "Other", "entry_id": "other"},
        )

        # Unloaded first, as Home Assistant does, then removed.
        await camera.async_will_remove_from_hass()
        await async_remove_entry(hass, coordinator.config_entry)

        assert list(issues(hass)) == ["preload_battery_camera_other"]
        await hass.async_stop(force=True)

    asyncio.run(run())
