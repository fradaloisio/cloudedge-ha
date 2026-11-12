"""Live-stream options: battery protection and audio."""
import threading
import time
from unittest.mock import Mock

import pytest

pytest.importorskip("homeassistant")

from custom_components.cloudedge import stream_bridge as module
from custom_components.cloudedge.stream_bridge import CloudEdgeStreamBridge, StreamOptions

from test_stream_recovery import H264_IDR, H264_PPS, H264_SPS

REAL_SLEEP = time.sleep
BATTERY_CAMERA = {
    "serial_number": "sn", "device_id": "id", "type_id": 1,
    "configuration": {"154": {"name": "BATTERY_PERCENT", "value": 80}},
}
MAINS_CAMERA = {
    "serial_number": "sn", "device_id": "id", "type_id": 1,
    "configuration": {"1007": {"name": "WIFI_STRENGTH", "value": 70}},
}
NOT_PASSED = object()


def wait_until(predicate, message, timeout=5.0):
    deadline = time.perf_counter() + timeout
    while not predicate():
        assert time.perf_counter() < deadline, message
        REAL_SLEEP(0.005)


class WatchedCamera:
    """A camera that streams for as long as the bridge keeps its session.

    Time is a manual clock. The bridge's admission, P2P worker and idle watch
    are the real ones, on their own threads; only the SDK session and the
    muxer are replaced."""

    def __init__(self, monkeypatch, options, device=BATTERY_CAMERA, keepalive_parameter=True):
        self.clock = [1000.0]
        self.viewers = 1
        self.sessions: list[dict] = []
        self.streamers: list[Mock] = []
        monkeypatch.setattr(module.time, "monotonic", lambda: self.clock[0])
        monkeypatch.setattr(module.time, "sleep", lambda seconds: REAL_SLEEP(min(seconds, 0.005)))
        monkeypatch.setattr(
            CloudEdgeStreamBridge, "client_count", property(lambda bridge: self.viewers)
        )
        coordinator = Mock()
        coordinator.client.OPENAPI_BASE_URL = "https://openapi.example.invalid"
        coordinator.client.create_streamer = (
            self.create_streamer if keepalive_parameter else self.create_streamer_of_an_older_library
        )
        coordinator.get_device_for_stream.return_value = device
        bridge = self.bridge = CloudEdgeStreamBridge(coordinator, "sn", options)
        bridge._stream_server = Mock()  # the local listener HA's stream worker uses
        for name, replacement in (
            ("_kick_prewake", lambda: None),
            ("_wait_for_prewake", lambda: True),
            ("_start_ffmpeg_muxer", lambda codec, **kwargs: True),
            ("_enqueue_video", lambda data, **kwargs: True),
        ):
            monkeypatch.setattr(bridge, name, replacement)

    def create_streamer(self, device, on_video, on_audio, on_login, on_disconnect,
                        video_id, manage_stream_switch=True, keepalive=NOT_PASSED):
        self.sessions.append({"keepalive": keepalive, "on_audio": on_audio})
        stopped = threading.Event()
        streamer = Mock()
        streamer.request_stop.side_effect = stopped.set

        def run():
            on_login()
            on_video(H264_SPS + H264_PPS + H264_IDR)
            streamer.streaming = True
            assert stopped.wait(10), "the bridge never ended the camera session"
            return 1, 20

        streamer.streaming = False
        streamer.run_session.side_effect = run
        self.streamers.append(streamer)
        return streamer

    def create_streamer_of_an_older_library(self, device, on_video, on_audio, on_login,
                                            on_disconnect, video_id, manage_stream_switch=True):
        return self.create_streamer(device, on_video, on_audio, on_login, on_disconnect, video_id)

    def view(self) -> bool:
        """HA opens the source; False when the bridge refuses the live view."""
        started = len(self.streamers)
        if not self.bridge._ensure_pipeline_started():
            return False
        wait_until(
            lambda: len(self.streamers) > started and self.streamers[-1].streaming,
            "the camera session never started",
        )
        return True

    def watch(self, seconds: float) -> None:
        """Let the viewer keep watching for `seconds` of the manual clock."""
        self.clock[0] += seconds
        REAL_SLEEP(0.1)  # several rounds of the bridge's one-second watch

    def rest(self) -> float:
        """Seconds the camera still rests after reaching its live view limit."""
        wait_until(lambda: not self.bridge.is_streaming, "the live view outlived its limit")
        diagnostics = self.bridge.diagnostics()
        assert diagnostics["stream_state"] == "live_limit"
        return diagnostics["stream_live_limit_rest"]

    def close(self) -> None:
        self.bridge.stop("manager_stop")


@pytest.fixture
def cameras(monkeypatch):
    created: list[WatchedCamera] = []

    def create(options, **kwargs) -> WatchedCamera:
        created.append(WatchedCamera(monkeypatch, options, **kwargs))
        return created[-1]

    yield create
    for camera in created:
        camera.close()


def test_battery_camera_live_view_stops_at_its_limit_then_rests(cameras):
    camera = cameras(StreamOptions(battery_max_live=600))
    assert camera.view()
    camera.watch(599)
    assert camera.bridge.is_streaming, "the live view ended before its limit"

    camera.watch(1)

    # The camera session is over, so the camera can sleep, and the state
    # says why for the whole rest.
    assert camera.rest() == 60
    assert camera.streamers[0].request_stop.called
    # The viewer never left: HA reconnects and is refused until the rest ends.
    camera.clock[0] += 59
    assert not camera.view()
    assert camera.rest() == 1
    camera.clock[0] += 1
    assert camera.view()
    assert camera.bridge.diagnostics()["stream_live_limit_rest"] == 0


def test_rest_doubles_while_the_same_viewer_stays_connected(cameras):
    # A dashboard left open or a preloaded stream reconnects after every
    # rest: a fixed rest would keep the camera awake most of the time.
    camera = cameras(StreamOptions(battery_max_live=600))
    rests = []
    for _ in range(8):
        assert camera.view(), "a view was refused outside the rest"
        camera.watch(600)
        rests.append(camera.rest())
        camera.clock[0] += rests[-1]
    assert rests == [60, 120, 240, 480, 960, 1920, 3600, 3600]


@pytest.mark.parametrize("ending", ["viewers-left", "profile-change", "much-later"])
def test_rest_restarts_from_a_minute_for_a_new_viewing(cameras, ending):
    camera = cameras(StreamOptions(battery_max_live=600))
    for expected in (60, 120):
        assert camera.view()
        camera.watch(600)
        assert camera.rest() == expected
        camera.clock[0] += expected
    if ending == "viewers-left":
        # A view that ends because nobody watches any more.
        assert camera.view()
        camera.viewers = 0
        wait_until(lambda: not camera.bridge.is_streaming, "the idle live view never stopped")
        assert camera.bridge.diagnostics()["stream_state"] != "live_limit"
        camera.viewers = 1
    elif ending == "profile-change":
        # The user's way to resume at once: it also ends a rest in progress.
        assert camera.view()
        camera.watch(600)
        assert camera.rest() == 240
        camera.bridge.set_stream_profile("SD")
        diagnostics = camera.bridge.diagnostics()
        assert (diagnostics["stream_state"], diagnostics["stream_live_limit_rest"]) == ("idle", 0)
    else:
        # Nobody reconnected when the rest ended: the next view is a new one.
        camera.clock[0] += 3600

    assert camera.view()
    camera.watch(600)
    assert camera.rest() == 60


@pytest.mark.parametrize(
    ("options", "device"),
    [
        (StreamOptions(battery_max_live=600), MAINS_CAMERA),
        (StreamOptions(battery_max_live=0), BATTERY_CAMERA),
    ],
    ids=["mains-camera", "limit-disabled"],
)
def test_live_view_without_a_limit_runs_while_someone_watches(cameras, options, device):
    camera = cameras(options, device=device)
    assert camera.view()

    camera.watch(24 * 3600)

    assert camera.bridge.is_streaming
    assert camera.bridge.diagnostics()["stream_live_limit_rest"] == 0


@pytest.mark.parametrize(
    ("options", "device", "keepalive"),
    [
        (StreamOptions(battery_keepalive=False), BATTERY_CAMERA, False),
        # On: the library's own default applies (as for every mains camera).
        (StreamOptions(battery_keepalive=True), BATTERY_CAMERA, NOT_PASSED),
        (StreamOptions(battery_keepalive=False), MAINS_CAMERA, NOT_PASSED),
    ],
    ids=["battery-off", "battery-on", "mains-off"],
)
def test_only_battery_cameras_stream_without_session_keepalives(
    cameras, options, device, keepalive
):
    camera = cameras(options, device=device)

    assert camera.view()

    assert camera.sessions[0]["keepalive"] is keepalive


def test_keepalive_option_is_ignored_by_a_library_without_it(cameras):
    camera = cameras(StreamOptions(battery_keepalive=False), keepalive_parameter=False)

    assert camera.view()

    assert camera.sessions[0]["keepalive"] is NOT_PASSED


@pytest.mark.parametrize("audio", [True, False])
def test_camera_audio_is_requested_only_when_enabled(cameras, audio):
    camera = cameras(StreamOptions(audio=audio))

    assert camera.view()

    assert (camera.sessions[0]["on_audio"] is not None) is audio
    codec = camera.bridge.diagnostics()["stream_audio_codec"]
    assert codec == ("pcm_mulaw/8000" if audio else None)
