"""Camera entity: attributes, still images and teardown of its HA Stream."""
import asyncio
import time
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

from custom_components.cloudedge.camera import CloudEdgeCamera

from test_coordinator import make_coordinator


def test_attributes_publish_the_configuration_by_parameter_name(tmp_path):
    # The configuration is keyed by numeric IoT code, but the filter compared
    # those keys with parameter names: no attribute was ever published.
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.data = {"sn": {"name": "Camera", "configuration": {
            "154": {"name": "BATTERY_PERCENT", "code": "154", "value": 75, "formatted": "75%"},
            "150": {"name": "MOTION_DET_ENABLE", "code": "150", "value": 1},
            "151": {"name": "MOTION_DET_SENSITIVITY", "code": "151", "value": 2},
        }}}
        camera = CloudEdgeCamera(coordinator, "sn", coordinator.data["sn"])

        attributes = camera.extra_state_attributes

        assert attributes["battery_percent"] == "75%"
        assert attributes["motion_det_enable"] == 1
        assert "motion_det_sensitivity" not in attributes
        assert not {"154", "150", "151"} & set(attributes)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("stream_state", "streaming", "ha_viewer", "live_image", "expected"),
    [
        ("streaming", True, True, b"live frame", b"live frame"),
        # No keyframe converted yet: keep the alarm snapshot.
        ("streaming", True, True, None, b"alarm snapshot"),
        # Idle camera: never start a stream for a still image.
        ("idle", False, True, b"live frame", b"alarm snapshot"),
        # The bridge streams for another viewer (go2rtc's WebRTC view) while
        # HA's own Stream is idle: its worker is not running either.
        ("streaming", True, False, b"live frame", b"alarm snapshot"),
    ],
)
def test_still_image_comes_from_the_running_live_stream(
    tmp_path, stream_state, streaming, ha_viewer, live_image, expected
):
    # Stills ignored a running live session and kept serving the last alarm
    # snapshot or the product icon while the camera was streaming.
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.data = {"sn": {
            "name": "Camera",
            "last_alarm_image": b"alarm snapshot",
            "last_alarm_time": time.time(),
        }}
        coordinator.get_stream_diagnostics = Mock(return_value={"stream_state": stream_state})
        coordinator.is_device_streaming = Mock(return_value=streaming)
        camera = CloudEdgeCamera(coordinator, "sn", coordinator.data["sn"])
        camera.hass = coordinator.hass
        stream = Mock()
        stream.outputs.return_value = {"hls": Mock()} if ha_viewer else {}
        stream.async_get_image = AsyncMock(return_value=live_image)
        camera.stream = stream

        assert await camera.async_camera_image(width=640, height=360) == expected
        assert camera.content_type == "image/jpeg"
        if streaming and ha_viewer:
            stream.async_get_image.assert_awaited_once_with(width=640, height=360)
        else:
            # Stream.async_get_image() starts the worker: on every dashboard
            # refresh it would wake a sleeping battery camera, or open a second
            # consumer of the camera another viewer is watching.
            stream.async_get_image.assert_not_awaited()

    asyncio.run(run())


def test_removed_camera_stops_its_home_assistant_stream(tmp_path):
    # HA core does not stop an entity's Stream on removal: after a reload the
    # orphaned worker kept retrying the closed bridge port, logging errors.
    from homeassistant.components.camera import DynamicStreamSettings

    async def run():
        coordinator = make_coordinator(tmp_path)
        camera = CloudEdgeCamera(coordinator, "sn", {"name": "Camera"})
        camera.hass = coordinator.hass
        # HA hands every Stream of an entity its one cached settings object,
        # the same one the camera preferences ("Preload stream") report.
        preferences = DynamicStreamSettings(preload_stream=True)
        stream = Mock()
        stream.dynamic_stream_settings = preferences
        stream.stop = AsyncMock()
        camera.stream = stream

        await camera.async_will_remove_from_hass()
        await coordinator.hass.async_block_till_done(wait_background_tasks=True)

        assert camera.stream is None
        # Stream.stop() keeps a preloaded worker running.
        assert stream.dynamic_stream_settings.preload_stream is False
        stream.stop.assert_awaited_once()
        # Only the Stream's own copy is cleared: clearing HA's object in place
        # turned the user's "Preload stream" off until Home Assistant restarted.
        assert preferences.preload_stream is True

    asyncio.run(run())
