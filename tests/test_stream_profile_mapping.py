"""HD, SD and Auto open the streams their names promise on real cameras.

Several battery cameras advertise their smallest profile first, so the
Android default is not the HD stream: HD opened 640x360, SD 2304x1296, and a
degraded Auto window moved to the heaviest stream.
"""
from unittest.mock import Mock

import pytest

pytest.importorskip("homeassistant")

from custom_components.cloudedge.stream_bridge import (
    CloudEdgeStreamBridge,
    CloudEdgeStreamManager,
)

# Capability v79 advertises the small profile first (Android default 100);
# v91 is adaptive (105).
V79_1080 = {"type_id": 5, "capability_version": 79, "bps2": {"0": "640x360", "1": "1920x1080"}}
V79_1296 = {"type_id": 5, "capability_version": 79, "bps2": {"0": "640x360", "2": "2304x1296"}}
V91_ADAPTIVE = {"type_id": 5, "capability_version": 91, "adb": 1,
                "bps2": {"0": "640x360", "2": "2304x1296"}}


def make_bridge(profile):
    coordinator = Mock()
    coordinator.client.OPENAPI_BASE_URL = "https://openapi.example.invalid"
    bridge = CloudEdgeStreamBridge(coordinator, "sn")
    bridge.set_stream_profile(profile)
    return bridge


def open_attempt(bridge, device):
    """The video_id the worker requests for its next P2P attempt."""
    return bridge._resolve_active_video_id(device)


def finish_attempt(bridge, video_id, frames=200):
    # A logged-in window Auto judges degraded (the 3.99 s gap seen live).
    bridge._record_stream_attempt(
        video_id=video_id, frames=frames, total_bytes=frames * 1000,
        duration=20.0, media_duration=17.5, max_frame_gap=3.99, logged_in=True,
    )


@pytest.mark.parametrize("device,hd,sd", [
    (V79_1080, 101, 100),
    (V79_1296, 102, 100),
    (V91_ADAPTIVE, 105, 100),
])
def test_hd_opens_the_largest_stream_and_sd_the_smallest(device, hd, sd):
    assert open_attempt(make_bridge("HD"), device) == hd
    assert open_attempt(make_bridge("SD"), device) == sd


@pytest.mark.parametrize("device", [V79_1080, V79_1296])
def test_auto_never_falls_back_to_a_heavier_stream(device):
    bridge = make_bridge("Auto")
    assert open_attempt(bridge, device) == 100
    # Degraded windows, then windows without any video.
    for frames in (200, 200, 200, 0, 0, 0):
        finish_attempt(bridge, 100, frames)
        assert open_attempt(bridge, device) == 100


def test_adaptive_auto_falls_back_to_the_small_stream_and_recovers():
    bridge = make_bridge("Auto")
    assert open_attempt(bridge, V91_ADAPTIVE) == 105
    finish_attempt(bridge, 105)
    finish_attempt(bridge, 105)
    assert open_attempt(bridge, V91_ADAPTIVE) == 100
    finish_attempt(bridge, 100, frames=0)
    finish_attempt(bridge, 100, frames=0)
    assert open_attempt(bridge, V91_ADAPTIVE) == 105


@pytest.mark.parametrize("device", [V79_1296, V91_ADAPTIVE])
@pytest.mark.parametrize("profile", ["HD", "SD"])
def test_active_profile_attribute_names_the_stream_actually_opened(device, profile):
    bridge = make_bridge(profile)
    open_attempt(bridge, device)
    assert bridge.diagnostics()["stream_profile_active"] == profile


def stream_ids(diagnostics):
    return {
        key: diagnostics[key]
        for key in (
            "stream_profile_active", "stream_video_id", "stream_main_video_id",
            "stream_substream_video_id", "stream_available_video_ids",
        )
    }


NO_STREAM = {
    "stream_profile_active": None, "stream_video_id": None,
    "stream_main_video_id": None, "stream_substream_video_id": None,
    "stream_available_video_ids": [],
}


def test_idle_camera_names_no_stream_until_a_session_opens_one():
    # Before its first session a camera's profiles are unknown. The attributes
    # claimed an open HD stream on ids 0 and 1, which cameras advertising the
    # 100 family do not even offer.
    manager = CloudEdgeStreamManager(Mock())
    assert stream_ids(manager.diagnostics("sn")) == NO_STREAM  # no bridge yet
    bridge = make_bridge("Auto")
    assert stream_ids(bridge.diagnostics()) == NO_STREAM

    open_attempt(bridge, V79_1296)
    opened = stream_ids(bridge.diagnostics())
    assert (opened["stream_main_video_id"], opened["stream_substream_video_id"]) == (102, 100)
    assert (opened["stream_profile_active"], opened["stream_video_id"]) == ("SD", 100)

    # A new choice names no stream until the next session opens it.
    bridge.set_stream_profile("HD")
    switched = stream_ids(bridge.diagnostics())
    assert (switched["stream_profile_active"], switched["stream_video_id"]) == (None, None)
    assert switched["stream_main_video_id"] == 102  # the camera's ids stay known
