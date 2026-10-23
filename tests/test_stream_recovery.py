"""Media recovery contracts at the bridge's transport and SDK boundaries."""
import queue
import socket
import threading
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("homeassistant")

from custom_components.cloudedge import stream_bridge as module
from custom_components.cloudedge.stream_bridge import CloudEdgeStreamBridge


H264_SPS = b"\x00\x00\x00\x01\x67\x42\x00\x1e"
H264_PPS = b"\x00\x00\x00\x01\x68\xce\x06\xe2"
H264_IDR = b"\x00\x00\x00\x01\x65\x88\x84"
HEVC_VPS = b"\x00\x00\x00\x01\x40\x01\xaa"
HEVC_SPS = b"\x00\x00\x00\x01\x42\x01\xbb"
HEVC_PPS = b"\x00\x00\x00\x01\x44\x01\xcc"
HEVC_IDR = b"\x00\x00\x00\x01\x26\x01\xdd"


def make_bridge():
    coordinator = Mock()
    coordinator.client.OPENAPI_BASE_URL = "https://openapi.example.invalid"
    coordinator.get_device_for_stream.return_value = {
        "serial_number": "sn", "device_id": "id", "type_id": 1,
    }
    bridge = CloudEdgeStreamBridge(coordinator, "sn")
    bridge._running = True
    bridge._session_generation = 1
    return bridge


def run_frames(monkeypatch, frames, bridge=None):
    bridge = bridge or make_bridge()
    delivered, codecs, login_states = [], [], []
    monkeypatch.setattr(bridge, "_wait_for_prewake", lambda: True)
    monkeypatch.setattr(bridge, "_should_keep_running", lambda: True)
    def enqueue_video(data, **kwargs):
        delivered.append(data)
        return True
    monkeypatch.setattr(bridge, "_enqueue_video", enqueue_video)
    def start_muxer(codec, **kwargs):
        codecs.append(codec)
        bridge._video_codec = codec
        return True
    monkeypatch.setattr(bridge, "_start_ffmpeg_muxer", start_muxer)
    def create_streamer(device, on_video, on_audio, on_login, on_disconnect,
                        video_id, manage_stream_switch=True):
        streamer = Mock(_cloudedge_native_signaling_retry=True)
        def run():
            on_login()
            login_states.append(bridge.diagnostics()["stream_state"])
            for frame in frames:
                on_video(frame)
            bridge._running = False
            return len(frames), sum(map(len, frames))
        streamer.run_session.side_effect = run
        return streamer
    bridge._coordinator.client.create_streamer = create_streamer
    bridge._stream_worker(1)
    return delivered, codecs, login_states


@pytest.mark.parametrize("frames,codec,prefix", [
    ([H264_SPS, H264_PPS, H264_IDR], "h264", H264_SPS + H264_PPS),
    ([HEVC_VPS, HEVC_SPS, HEVC_PPS, HEVC_IDR], "hevc", HEVC_VPS + HEVC_SPS + HEVC_PPS),
])
def test_separate_parameter_sets_are_accumulated_until_an_actual_idr(monkeypatch, frames, codec, prefix):
    delivered, codecs, _ = run_frames(monkeypatch, frames)
    assert codecs == [codec]
    assert delivered == [prefix + frames[-1]]


HEVC_PARAMETER_SETS = HEVC_VPS + HEVC_SPS + HEVC_PPS
HEVC_IDR_N_LP = b"\x00\x00\x00\x01\x28\x01\xdd"  # type 20: an IDR without leading pictures
HEVC_TRAIL_R = b"\x00\x00\x00\x01\x02\x01\xee"  # type 1: an ordinary predicted picture
HEVC_STSA_N = b"\x00\x00\x00\x01\x08\x01\xee"  # type 4: a sub-layer switching picture
HEVC_EOS = b"\x00\x00\x00\x01\x48\x01"  # type 36: end of sequence


@pytest.mark.parametrize("frames", [
    # An encoder without B frames marks every IDR as IDR_N_LP (x265 does).
    [HEVC_PARAMETER_SETS + HEVC_IDR_N_LP, *[HEVC_TRAIL_R] * 9] * 3,
    # STSA_N and EOS inside a GOP are ordinary HEVC NAL units.
    [
        HEVC_PARAMETER_SETS + HEVC_IDR, *[HEVC_TRAIL_R] * 4, HEVC_STSA_N,
        *[HEVC_TRAIL_R] * 3, HEVC_TRAIL_R + HEVC_EOS,
        HEVC_PARAMETER_SETS + HEVC_IDR, *[HEVC_TRAIL_R] * 9,
    ],
], ids=["idr-n-lp", "stsa-n-and-eos"])
def test_hevc_units_that_share_h264_pps_bits_keep_the_hevc_stream(monkeypatch, frames):
    # Their headers (0x28, 0x08, 0x48) carry the H.264 PPS type in their low
    # five bits. Read as H.264, an IDR_N_LP stream never started; a STSA_N or
    # EOS became a codec change that discarded video until the next keyframe.
    delivered, codecs, _ = run_frames(monkeypatch, frames)
    assert codecs == ["hevc"]
    assert len(delivered) == len(frames)


def test_login_without_video_is_still_connecting(monkeypatch):
    bridge = make_bridge()
    _, _, login_states = run_frames(monkeypatch, [], bridge)
    assert login_states == ["connecting"]
    assert bridge._consecutive_stream_failures == 1


def test_h264_predictive_slice_does_not_change_codec(monkeypatch):
    first = H264_SPS + H264_PPS + H264_IDR
    predicted = b"\x00\x00\x00\x01\x41\x9a\x21\x42"
    delivered, codecs, _ = run_frames(monkeypatch, [first, predicted])
    assert codecs == ["h264"]
    assert delivered == [first, predicted]


def test_authenticated_sessions_without_valid_video_reach_retry_cooldown(monkeypatch):
    bridge = make_bridge()
    monkeypatch.setattr(bridge, "_wait_for_prewake", lambda: True)
    monkeypatch.setattr(bridge, "_should_keep_running", lambda: True)
    monkeypatch.setattr(CloudEdgeStreamBridge, "client_count", property(lambda self: 1))
    monkeypatch.setattr(module, "_RECONNECT_COOLDOWN", 0)
    attempts = []
    def create_streamer(device, on_video, on_audio, on_login, on_disconnect,
                        video_id, manage_stream_switch=True):
        streamer = Mock(_cloudedge_native_signaling_retry=True)
        def run():
            attempts.append(video_id)
            if len(attempts) > module._MAX_CONSECUTIVE_STREAM_FAILURES:
                bridge._running = False
                return 0, 0
            on_login()
            # Logged in and receiving an incomplete GOP is still unusable.
            on_video(H264_SPS)
            on_video(b"\x00\x00\x00\x01\x41\x9a\x21\x42")
            return 2, 16
        streamer.run_session.side_effect = run
        return streamer
    bridge._coordinator.client.create_streamer = create_streamer
    bridge._stream_worker(1)
    assert len(attempts) == module._MAX_CONSECUTIVE_STREAM_FAILURES
    assert bridge._consecutive_stream_failures == len(attempts)
    assert bridge._failure_cooldown_until > module.time.monotonic()


class SilentCamera:
    """Logs in but sends no video unless given frames for its next attempt.

    Time is a manual clock. A view runs the bridge's real admission and
    worker; the worker runs on the test thread until its session ends."""

    def __init__(self, monkeypatch):
        self.clock = [1000.0]
        self.next_frames: list[bytes] = []
        monkeypatch.setattr(module.time, "monotonic", lambda: self.clock[0])
        monkeypatch.setattr(module, "_RECONNECT_COOLDOWN", 0)
        monkeypatch.setattr(CloudEdgeStreamBridge, "client_count", property(lambda self: 1))
        bridge = self.bridge = make_bridge()
        bridge._running = False
        bridge._stream_server = Mock()  # the local listener HA's stream worker uses
        for name, replacement in (
            ("_kick_prewake", lambda: None),
            ("_wait_for_prewake", lambda: True),
            ("_idle_watch_loop", lambda gen: None),
            ("_should_keep_running", lambda: True),
            ("_start_ffmpeg_muxer", lambda codec, **kwargs: True),
            ("_enqueue_video", lambda data, **kwargs: True),
        ):
            monkeypatch.setattr(bridge, name, replacement)
        bridge._coordinator.client.create_streamer = self.create_streamer

    def create_streamer(self, device, on_video, on_audio, on_login, on_disconnect,
                        video_id, manage_stream_switch=True):
        streamer = Mock(_cloudedge_native_signaling_retry=True)

        def run():
            on_login()
            frames, self.next_frames = self.next_frames, []
            for frame in frames:
                on_video(frame)
            return len(frames), sum(map(len, frames))

        streamer.run_session.side_effect = run
        return streamer

    def view(self) -> bool:
        """HA opens the source; False when the bridge refuses the session."""
        sessions = []
        self.bridge._stream_worker = sessions.append
        try:
            if not self.bridge._ensure_pipeline_started():
                return False
            self.bridge._worker_thread.join(5)
        finally:
            del self.bridge._stream_worker
        CloudEdgeStreamBridge._stream_worker(self.bridge, sessions[0])
        return True

    def pause(self) -> float:
        diagnostics = self.bridge.diagnostics()
        assert diagnostics["stream_state"] == "retry_cooldown"
        return diagnostics["stream_retry_cooldown"]


def test_failure_pause_starts_at_a_minute_and_doubles_up_to_ten(monkeypatch):
    camera = SilentCamera(monkeypatch)
    pauses = []
    for _ in range(6):
        assert camera.view(), "a view was refused outside the pause"
        # For the whole pause the state says so (it read 'worker_exit'),
        # with the seconds left, and HA's reconnects are refused.
        pauses.append(camera.pause())
        camera.clock[0] += pauses[-1] / 2
        assert not camera.view()
        assert camera.pause() == pauses[-1] / 2
        camera.clock[0] += pauses[-1] / 2
        assert camera.bridge.diagnostics()["stream_state"] == "idle"
    # Four attempts without video start it; each further one doubles it.
    assert pauses == [60, 120, 240, 480, 600, 600]


@pytest.mark.parametrize("reset", ["first-video-frame", "profile-change"])
def test_failure_pause_restarts_from_a_minute_after_a_reset(monkeypatch, reset):
    camera = SilentCamera(monkeypatch)
    for _ in range(2):
        assert camera.view()
        pause = camera.pause()
        camera.clock[0] += pause
    camera.view()
    assert camera.pause() == 240
    if reset == "first-video-frame":
        camera.clock[0] += 240
        camera.next_frames = [H264_SPS + H264_PPS + H264_IDR]
    else:
        camera.bridge.set_stream_profile("SD")
        diagnostics = camera.bridge.diagnostics()
        assert diagnostics["stream_retry_cooldown"] == 0
        assert diagnostics["stream_state"] == "idle"
    # The camera works again (or the user changed profile): a later failure
    # streak earns the first, shortest pause again.
    assert camera.view()
    assert camera.pause() == 60


def test_views_abandoned_before_video_never_pause_the_camera(monkeypatch):
    # A viewer who leaves while a battery camera is still waking ends the
    # attempt from the bridge (idle watch). That says nothing about the
    # camera; counting those stops paused the fifth view for ten minutes.
    bridge = make_bridge()
    bridge._running = False
    bridge._stream_server = Mock()  # the local listener HA's stream worker uses
    viewer = [False]
    monkeypatch.setattr(
        CloudEdgeStreamBridge, "client_count", property(lambda self: int(viewer[0]))
    )
    monkeypatch.setattr(module, "_IDLE_TIMEOUT", 0.0)
    monkeypatch.setattr(bridge, "_kick_prewake", lambda: None)
    monkeypatch.setattr(bridge, "_wait_for_prewake", lambda: True)
    real_sleep = module.time.sleep
    monkeypatch.setattr(module.time, "sleep", lambda seconds: real_sleep(min(seconds, 0.01)))

    def create_streamer(device, on_video, on_audio, on_login, on_disconnect,
                        video_id, manage_stream_switch=True):
        stopped = threading.Event()
        streamer = Mock(_cloudedge_native_signaling_retry=True)
        streamer.request_stop.side_effect = stopped.set

        def run():
            on_login()
            viewer[0] = False  # the viewer leaves before the first frame
            assert stopped.wait(5), "the idle watch never stopped the attempt"
            return 0, 0

        streamer.run_session.side_effect = run
        return streamer

    bridge._coordinator.client.create_streamer = create_streamer
    for view in range(module._MAX_CONSECUTIVE_STREAM_FAILURES + 1):
        viewer[0] = True
        assert bridge._ensure_pipeline_started(), f"view {view + 1} was rejected"
        threads = (bridge._worker_thread, bridge._idle_watch_thread)
        for thread in threads:
            thread.join(5)
        assert not any(thread.is_alive() for thread in threads)
    assert bridge._consecutive_stream_failures == 0
    assert bridge.diagnostics()["stream_retry_cooldown"] == 0


@pytest.mark.parametrize("profiles", [["SD"], ["SD", "HD"]], ids=["once", "twice"])
def test_attempt_retired_by_a_profile_change_is_not_a_camera_failure(
    monkeypatch, profiles
):
    bridge = make_bridge()
    monkeypatch.setattr(bridge, "_wait_for_prewake", lambda: True)
    monkeypatch.setattr(bridge, "_should_keep_running", lambda: True)
    monkeypatch.setattr(CloudEdgeStreamBridge, "client_count", property(lambda self: 1))
    streamer_runs, failures_at_next_attempt = [], []

    def create_streamer(device, on_video, on_audio, on_login, on_disconnect,
                        video_id, manage_stream_switch=True):
        streamer = Mock(_cloudedge_native_signaling_retry=True)
        first = not streamer_runs

        def run():
            streamer_runs.append(video_id)
            if first:
                on_login()
                # The user picks another profile while the camera is still
                # waking: the bridge itself retires this attempt. A second
                # pick while it is still closing stops it again.
                for profile in profiles:
                    bridge.set_stream_profile(profile)
            else:
                failures_at_next_attempt.append(bridge._consecutive_stream_failures)
                bridge._running = False
            return 0, 0

        streamer.run_session.side_effect = run
        return streamer

    bridge._coordinator.client.create_streamer = create_streamer
    bridge._stream_worker(1)
    assert len(streamer_runs) == 2
    assert failures_at_next_attempt == [0]


def test_attempt_retired_by_a_profile_change_then_the_idle_watch_is_not_a_camera_failure(
    monkeypatch,
):
    # The viewer picks another profile, then leaves while the retired attempt
    # is still closing: the idle watch stops that attempt a second time.
    bridge = make_bridge()
    viewer = [1]
    monkeypatch.setattr(
        CloudEdgeStreamBridge, "client_count", property(lambda self: viewer[0])
    )
    monkeypatch.setattr(module, "_IDLE_TIMEOUT", 0.0)
    monkeypatch.setattr(bridge, "_wait_for_prewake", lambda: True)
    real_sleep = module.time.sleep
    monkeypatch.setattr(module.time, "sleep", lambda seconds: real_sleep(min(seconds, 0.01)))
    stops = []
    stopped_again = threading.Event()

    def request_stop():
        stops.append(threading.current_thread())
        if len(stops) >= 2:
            stopped_again.set()

    def create_streamer(device, on_video, on_audio, on_login, on_disconnect,
                        video_id, manage_stream_switch=True):
        streamer = Mock(_cloudedge_native_signaling_retry=True)
        streamer.request_stop.side_effect = request_stop

        def run():
            on_login()
            bridge.set_stream_profile("SD")
            viewer[0] = 0
            stopped_again.wait(5)
            return 0, 0

        streamer.run_session.side_effect = run
        return streamer

    bridge._coordinator.client.create_streamer = create_streamer
    idle_watch = threading.Thread(target=bridge._idle_watch_loop, args=(1,), daemon=True)
    idle_watch.start()
    bridge._stream_worker(1)
    idle_watch.join(5)
    assert not idle_watch.is_alive()
    assert idle_watch in stops, "the idle watch never stopped the retired attempt"
    assert bridge._consecutive_stream_failures == 0


def test_profile_change_before_the_streamer_is_published_restarts_only_the_attempt(
    monkeypatch,
):
    bridge = make_bridge()
    monkeypatch.setattr(bridge, "_wait_for_prewake", lambda: True)
    monkeypatch.setattr(bridge, "_should_keep_running", lambda: True)
    created, requested_ids = [], []

    def create_streamer(device, on_video, on_audio, on_login, on_disconnect,
                        video_id, manage_stream_switch=True):
        streamer = Mock(_cloudedge_native_signaling_retry=True)
        created.append(streamer)
        requested_ids.append(video_id)
        if len(created) == 1:
            # Switched while the SDK was still preparing this session.
            bridge.set_stream_profile("SD")
        else:
            def run():
                bridge._running = False
                return 0, 0
            streamer.run_session.side_effect = run
        return streamer

    bridge._coordinator.client.create_streamer = create_streamer
    bridge._stream_worker(1)
    assert len(created) == 2, "the profile change ended the whole viewing session"
    created[0].request_stop.assert_called()
    created[0].run_session.assert_not_called()
    created[1].run_session.assert_called_once()
    assert requested_ids[1] == bridge._sd_video_id


def test_new_attempt_redetects_codec_and_ignores_retired_callbacks(monkeypatch):
    bridge = make_bridge()
    delivered, codecs, retired_callbacks, states = [], [], [], []
    monkeypatch.setattr(bridge, "_wait_for_prewake", lambda: True)
    monkeypatch.setattr(bridge, "_should_keep_running", lambda: True)
    monkeypatch.setattr(CloudEdgeStreamBridge, "client_count", property(lambda self: 1))
    monkeypatch.setattr(module, "_RECONNECT_COOLDOWN", 0)
    def enqueue(data, **kwargs):
        delivered.append(data)
        return True
    def start(codec, **kwargs):
        codecs.append(codec)
        bridge._video_codec = codec
        return True
    monkeypatch.setattr(bridge, "_enqueue_video", enqueue)
    monkeypatch.setattr(bridge, "_start_ffmpeg_muxer", start)
    def create_streamer(device, on_video, on_audio, on_login, on_disconnect,
                        video_id, manage_stream_switch=True):
        streamer = Mock(_cloudedge_native_signaling_retry=True)
        attempt = len(retired_callbacks)
        def run():
            on_login()
            if not attempt:
                on_video(H264_SPS + H264_PPS + H264_IDR)
                retired_callbacks.append((on_video, on_login, on_disconnect))
            else:
                on_video(HEVC_VPS)
                on_video(HEVC_SPS)
                on_video(HEVC_PPS)
                on_video(HEVC_IDR)
                old_video, old_login, old_disconnect = retired_callbacks[0]
                old_video(H264_SPS + H264_PPS + H264_IDR)
                old_login()
                old_disconnect()
                states.append(bridge.diagnostics()["stream_state"])
                bridge._running = False
            return 4, 64
        streamer.run_session.side_effect = run
        return streamer
    bridge._coordinator.client.create_streamer = create_streamer
    bridge._stream_worker(1)
    assert codecs == ["h264", "hevc"]
    assert delivered == [H264_SPS + H264_PPS + H264_IDR,
                         HEVC_VPS + HEVC_SPS + HEVC_PPS + HEVC_IDR]
    assert states == ["streaming"]


H264_SPS_RESIZED = b"\x00\x00\x00\x01\x67\x4d\x00\x28"  # another resolution
H264_P = b"\x00\x00\x00\x01\x41\x9a\x21\x42"
FIRST_FORMAT_IDR = H264_SPS + H264_PPS + H264_IDR
RESIZED_IDR = H264_SPS_RESIZED + H264_PPS + H264_IDR


def manual_clock(monkeypatch, start, on_sleep):
    """time.monotonic() and time.sleep() for the test thread: a sleep only
    advances the clock, then calls on_sleep(now). Other threads, such as
    leftovers of earlier tests, keep real time."""
    test_thread = threading.current_thread()
    real_monotonic, real_sleep = module.time.monotonic, module.time.sleep
    clock = [start]

    def monotonic():
        if threading.current_thread() is not test_thread:
            return real_monotonic()
        return clock[0]

    def sleep(seconds):
        if threading.current_thread() is not test_thread:
            return real_sleep(seconds)
        clock[0] += seconds
        on_sleep(clock[0])

    monkeypatch.setattr(module.time, "monotonic", monotonic)
    monkeypatch.setattr(module.time, "sleep", sleep)
    return clock


class MuxerProcesses:
    """Replace only the FFmpeg process that _start_ffmpeg_muxer_locked spawns.

    Records, in order, each muxer start and kill, each request for HA's fast
    restart (the coordinator's answer is scripted) and each end of HA's
    current source. The format check, the notification order, retirement
    and the keyframe gate stay the bridge's own."""

    def __init__(self, monkeypatch, bridge, *, ha_answers=True):
        self.bridge = bridge
        self.events = []
        self.ha_answers = ha_answers
        bridge._coordinator.notify_stream_source_changed = self.notify
        close_source = bridge._close_stream_consumers

        def close():
            self.events.append(("close",))
            close_source()

        monkeypatch.setattr(bridge, "_close_stream_consumers", close)
        monkeypatch.setattr(bridge, "_start_ffmpeg_muxer_locked", self.start)

    def start(self, codec):
        index = sum(event[0] == "start" for event in self.events)
        proc = Mock(stdin=None, stdout=None, stderr=None, returncode=None)
        proc.poll.side_effect = lambda: proc.returncode

        def kill():
            self.events.append(("kill", index))
            proc.returncode = -9

        proc.kill.side_effect = kill
        self.bridge._ffmpeg_proc = proc
        # A queue per muxer, as each real one, without its 1 s age limit:
        # the tests read it after the camera's script, however long it took.
        self.bridge._video_queue = module.VideoBuffer(max_age=float("inf"))
        self.events.append(("start", index))
        return True

    def notify(self, serial_number, close_consumers):
        self.events.append(("notify", serial_number))
        if self.ha_answers:
            close_consumers()  # HA's restart is armed: its old source may end
        return self.ha_answers


def queued(buffer):
    """The frames waiting in a VideoBuffer for its muxer, in order."""
    frames = []
    while True:
        try:
            frames.append(buffer.get_nowait()[0])
        except queue.Empty:
            return frames


@pytest.mark.parametrize("ha_answers", [True, False], ids=["ha-restarts", "ha-unreachable"])
def test_reconnected_viewer_never_receives_retired_muxer_output(monkeypatch, ha_answers):
    bridge = make_bridge()
    MuxerProcesses(monkeypatch, bridge, ha_answers=ha_answers)
    assert bridge._start_ffmpeg_muxer("h264", parameter_sets=H264_SPS + H264_PPS)
    old_proc = bridge._ffmpeg_proc
    reading, release = threading.Event(), threading.Event()

    class PendingOutput:
        def read1(self, size):
            if reading.is_set():
                return b""
            reading.set()
            if not release.wait(3):
                return b""
            return b"old-format-packets"

        def close(self):
            pass  # an already in-flight read can finish after retirement

    old_proc.stdout = PendingOutput()
    reader = threading.Thread(target=bridge._ffmpeg_stdout_reader, args=(old_proc,))
    writer, viewer = socket.socketpair()
    viewer.settimeout(3)
    close_consumers = bridge._close_stream_consumers
    reconnected = []

    def reconnect_after_eof():
        close_consumers()
        if not reconnected:
            reconnected.append(bridge._register_stream_client(writer))
            # HA reconnects while the format-change notification is returning;
            # release the old FFmpeg read into the newly registered viewer.
            release.set()
            reader.join(3)

    monkeypatch.setattr(bridge, "_close_stream_consumers", reconnect_after_eof)
    reader.start()
    try:
        assert reading.wait(3)
        assert bridge._start_ffmpeg_muxer("hevc", parameter_sets=HEVC_PARAMETER_SETS)
        assert reconnected == [True]
        assert not reader.is_alive()
        bridge._broadcast_stream(b"new-format-packets")
        close_consumers()
        received = bytearray()
        while chunk := viewer.recv(1024):
            received.extend(chunk)
        assert received == b"new-format-packets"
    finally:
        release.set()
        bridge.stop("manager_stop")
        viewer.close()
        writer.close()
        reader.join(3)


@pytest.mark.parametrize("ha_answers", [True, False], ids=["ha-restarts", "ha-unreachable"])
def test_mid_attempt_format_change_asks_ha_once_then_starts_a_new_muxer(
    monkeypatch, ha_answers
):
    # The camera changes resolution within an attempt. FFmpeg was configured
    # from the old parameter sets, so the next IDR needs a new muxer, and HA
    # must arm its fast restart before the old source ends: EOF first sends
    # its stream worker into retry backoff. HA is asked once per change: when
    # its loop did not answer, asking again at every IDR of the new format
    # blocked this P2P thread for the whole wait each time. The bridge then
    # ends the old source itself and HA reconnects through that backoff.
    bridge = make_bridge()
    monkeypatch.setattr(bridge, "_wait_for_prewake", lambda: True)
    monkeypatch.setattr(bridge, "_should_keep_running", lambda: True)
    processes = MuxerProcesses(monkeypatch, bridge, ha_answers=ha_answers)
    seen = {}

    def create_streamer(device, on_video, on_audio, on_login, on_disconnect,
                        video_id, manage_stream_switch=True):
        streamer = Mock(_cloudedge_native_signaling_retry=True)

        def run():
            on_login()
            for frame in (FIRST_FORMAT_IDR, H264_P, RESIZED_IDR, H264_P,
                          RESIZED_IDR, H264_P):
                on_video(frame)
            seen.update(events=list(processes.events),
                        muxer_input=queued(bridge._video_queue))
            bridge._running = False
            return 6, 0

        streamer.run_session.side_effect = run
        return streamer

    bridge._coordinator.client.create_streamer = create_streamer
    bridge._stream_worker(1)
    assert seen["events"] == [
        ("start", 0), ("kill", 0), ("notify", "sn"), ("close",), ("start", 1),
    ]
    assert seen["muxer_input"] == [RESIZED_IDR, H264_P, RESIZED_IDR, H264_P]


@pytest.mark.parametrize("ha", ["reconnects", "gone", "unreachable"],
                         ids=["ha-reconnects", "viewer-gone", "ha-unreachable"])
def test_attempt_ending_in_a_format_change_waits_briefly_for_ha(monkeypatch, ha):
    # The change closes HA's source on purpose and HA reopens it at once. If
    # the camera's window ends in between, the worker must wait for that
    # reconnect rather than end the viewing session, but only briefly:
    # without a viewer it must not keep the camera awake. When HA's loop did
    # not answer, no fast restart was armed: HA reconnects only after its
    # retry backoff, and the worker does not wait for it at all.
    viewers = [1]
    reconnect_at = []

    def on_sleep(now):
        if reconnect_at and now >= reconnect_at[0]:
            viewers[0] = 1
        assert now < 1060, "the worker kept the camera awake for nobody"

    clock = manual_clock(monkeypatch, 1000.0, on_sleep)
    monkeypatch.setattr(module, "_RECONNECT_COOLDOWN", 0)
    monkeypatch.setattr(
        CloudEdgeStreamBridge, "client_count", property(lambda self: viewers[0])
    )
    bridge = make_bridge()
    monkeypatch.setattr(bridge, "_wait_for_prewake", lambda: True)

    def source_closed():
        viewers[0] = 0  # HA's worker lets go of the old source...
        if ha == "reconnects":
            reconnect_at.append(clock[0] + 1.0)  # ...and opens it again

    monkeypatch.setattr(bridge, "_close_stream_consumers", source_closed)
    MuxerProcesses(monkeypatch, bridge, ha_answers=ha != "unreachable")
    attempts = []

    def create_streamer(device, on_video, on_audio, on_login, on_disconnect,
                        video_id, manage_stream_switch=True):
        attempts.append(video_id)
        streamer = Mock(_cloudedge_native_signaling_retry=True)

        def run():
            if len(attempts) == 1:
                on_login()
                on_video(FIRST_FORMAT_IDR)
                on_video(RESIZED_IDR)  # then the camera's live window ends
                return 2, 0
            bridge._running = False
            return 0, 0

        streamer.run_session.side_effect = run
        return streamer

    bridge._coordinator.client.create_streamer = create_streamer
    bridge._stream_worker(1)
    assert len(attempts) == (2 if ha == "reconnects" else 1)
    if ha == "unreachable":
        assert clock[0] == 1000.0, "the worker waited for a reconnect HA was never told of"


def test_stop_during_wake_cannot_enable_live_afterwards(monkeypatch):
    bridge = make_bridge()
    sessions = []
    def create_streamer(device, on_video, on_audio, on_login, on_disconnect,
                        video_id, manage_stream_switch=True):
        # Only recorded: the worker's own error handling would swallow an
        # assertion raised here. The test thread checks it after the join.
        sessions.append(video_id)
        return Mock(_cloudedge_native_signaling_retry=True)
    bridge._coordinator.client.create_streamer = create_streamer
    entered, release = threading.Event(), threading.Event()
    def wake():
        entered.set()
        assert release.wait(5)
        return True
    monkeypatch.setattr(bridge, "_wait_for_prewake", wake)
    monkeypatch.setattr(bridge, "_should_keep_running", lambda: True)
    thread = threading.Thread(target=bridge._stream_worker, args=(1,))
    try:
        thread.start()
        assert entered.wait(5)
        bridge.stop()
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert sessions == [], "a worker stopped during the wake opened a P2P session"
        bridge._coordinator.client.set_device_config.assert_not_called()
    finally:
        release.set()
        thread.join(5)


@pytest.mark.parametrize("config_write", [True, RuntimeError("OpenAPI error")])
def test_live_view_session_leaves_front_light_alone(monkeypatch, config_write):
    # IoT 167 is FRONT_LIGHT_SWITCH. A viewing session must neither write it
    # nor let the SDK toggle it (pycloudedge <= 0.1.11 defaults to True), also
    # when a config write would fail.
    bridge = make_bridge()
    client = bridge._coordinator.client
    if isinstance(config_write, Exception):
        client.set_device_config.side_effect = config_write
    else:
        client.set_device_config.return_value = config_write
    sdk_flags = []
    monkeypatch.setattr(bridge, "_wait_for_prewake", lambda: True)
    monkeypatch.setattr(bridge, "_should_keep_running", lambda: True)
    monkeypatch.setattr(bridge, "_enqueue_video", lambda data, **kwargs: True)
    monkeypatch.setattr(bridge, "_start_ffmpeg_muxer", lambda codec, **kwargs: True)

    def create_streamer(device, on_video, on_audio, on_login, on_disconnect,
                        video_id, manage_stream_switch=True):
        sdk_flags.append(manage_stream_switch)
        streamer = Mock(_cloudedge_native_signaling_retry=True)

        def run():
            on_login()
            on_video(H264_SPS + H264_PPS + H264_IDR)
            bridge._running = False
            return 1, 23

        streamer.run_session.side_effect = run
        return streamer

    client.create_streamer = create_streamer
    bridge._stream_worker(1)

    assert sdk_flags == [False]
    client.set_device_config.assert_not_called()


def test_video_backlog_discards_a_whole_gop_and_waits_for_fresh_idr(monkeypatch):
    bridge = make_bridge()
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    bridge._enqueue_video(H264_SPS + H264_PPS + H264_IDR, reset_bootstrap=True)
    for index in range(50):
        clock[0] += 0.05
        bridge._enqueue_video(b"old-p" + bytes([index]))
    clock[0] += 1
    bridge._enqueue_video(b"fresh-idr", reset_bootstrap=True)
    bridge._enqueue_video(b"fresh-p")
    received = []
    while True:
        try:
            received.append(bridge._video_queue.get_nowait()[0])
        except queue.Empty:
            break
    assert received == [b"fresh-idr", b"fresh-p"]


def test_frames_the_buffer_refuses_still_count_as_received(monkeypatch):
    # With FFmpeg behind, VideoBuffer retires the GOP and refuses P frames
    # until the next IDR. Those frames still arrived on time: the attempt's
    # rate and gaps (Auto's verdict, the pacer's cadence) describe the
    # network, never the bridge's own drops, which have their own counter.
    bridge = make_bridge()
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(bridge, "_wait_for_prewake", lambda: True)
    monkeypatch.setattr(bridge, "_should_keep_running", lambda: True)
    monkeypatch.setattr(bridge, "_start_ffmpeg_muxer", lambda codec, **kwargs: True)
    keyframe = H264_SPS + H264_PPS + H264_IDR
    predicted = b"\x00\x00\x00\x01\x41\x9a\x21\x42"
    frames = [keyframe if index % 30 == 0 else predicted for index in range(90)]
    refused = []
    admit = bridge._enqueue_video

    def enqueue(data, **kwargs):
        accepted = admit(data, **kwargs)
        if not accepted:
            refused.append(data)
        return accepted

    monkeypatch.setattr(bridge, "_enqueue_video", enqueue)

    def create_streamer(device, on_video, on_audio, on_login, on_disconnect,
                        video_id, manage_stream_switch=True):
        streamer = Mock(_cloudedge_native_signaling_retry=True)

        def run():
            on_login()
            for frame in frames:  # a steady 15 fps camera; nothing drains
                on_video(frame)
                clock[0] += 1 / 15
            bridge._running = False
            return len(frames), sum(map(len, frames))

        streamer.run_session.side_effect = run
        return streamer

    bridge._coordinator.client.create_streamer = create_streamer
    bridge._stream_worker(1)

    assert refused, "the buffer should have retired the stale video"
    diagnostics = bridge.diagnostics()
    assert diagnostics["stream_last_frames"] == len(frames)
    assert diagnostics["stream_last_fps"] == pytest.approx(15, rel=0.01)
    assert diagnostics["stream_last_max_gap"] < 0.1
    assert diagnostics["stream_dropped_frames"] == len(refused)


def test_dropped_frames_restart_from_zero_for_a_new_viewing_session(monkeypatch):
    bridge = make_bridge()
    bridge._running = False
    bridge._stream_server = Mock()  # the local listener HA's stream worker uses
    monkeypatch.setattr(bridge, "_kick_prewake", lambda: None)
    monkeypatch.setattr(bridge, "_stream_worker", lambda gen: None)
    monkeypatch.setattr(bridge, "_idle_watch_loop", lambda gen: None)
    predicted = b"\x00\x00\x00\x01\x41\x9a\x21\x42"

    assert bridge._ensure_pipeline_started()
    for _ in range(3):  # no IDR yet: VideoBuffer refuses predicted frames
        assert not bridge._enqueue_video(predicted)
    assert bridge.diagnostics()["stream_dropped_frames"] == 3
    bridge.stop("worker_exit")  # keeps the listener for HA's next connection

    assert bridge._ensure_pipeline_started()
    assert bridge.diagnostics()["stream_dropped_frames"] == 0


class SimulatedCamera:
    """Simulated time for the pacer: it passes only while the bridge sleeps
    or writes, and frames reach the bridge's real VideoBuffer at their
    arrival times. Events (time, action) run when their time comes."""

    def __init__(self, bridge, arrivals, events=()):
        self.now = 0.0
        self.pending = deque(arrivals)
        self.events = deque(sorted(events, key=lambda event: event[0]))
        self.bridge = bridge
        camera = self

        class Buffer(module.VideoBuffer):
            def get(self, timeout=0.0):
                # Waiting on an empty queue lasts until the next arrival.
                if self.empty():
                    if camera.pending:
                        due = camera.pending[0][0]
                        if camera.events:
                            due = min(due, camera.events[0][0])
                        camera.sleep(min(timeout, due - camera.now))
                    else:
                        bridge._running = False
                return super().get(0.0)

        self.buffer = bridge._video_queue = Buffer()

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, seconds)
        while self.events and self.events[0][0] <= self.now:
            self.events.popleft()[1]()
        while self.pending and self.pending[0][0] <= self.now:
            due, data, keyframe = self.pending.popleft()
            # As on_video: the receipt clock, then admission.
            self.bridge._last_real_video = due
            self.bridge._enqueue_video(data, reset_bootstrap=keyframe)


class SimulatedWriter:
    def __init__(self, camera, write_seconds):
        self.camera = camera
        self.write_seconds = write_seconds
        self.written = []
        self.times = []
        # (stall clock, bootstrap reset) as the muxer's stall watchdog and
        # stdout reader see them during each write.
        self.seen = []

    def write(self, data):
        bridge = self.camera.bridge
        self.times.append(self.camera.now)
        self.seen.append((bridge._video_write_started, bridge._bootstrap_reset.is_set()))
        bridge._bootstrap_reset.clear()  # as the stdout reader at FFmpeg's next output
        self.camera.sleep(self.write_seconds)
        self.written.append(data)

    def flush(self):
        pass


@pytest.mark.parametrize("network", ["burst-after-pause", "write-cost"])
def test_pacer_keeps_up_with_the_camera_without_dropping_a_gop(monkeypatch, network):
    # KCP delivers the frames held back by a network pause in one burst, and
    # every write to FFmpeg takes time. A pacer that writes at the source
    # rate at best never works off that backlog: past VideoBuffer's 1 s age
    # limit the whole GOP is retired and video freezes until the next IDR.
    fps, gop = 15.0, 60
    if network == "burst-after-pause":
        seconds, pause, write_seconds = 16, (6.0, 7.5), 0.0
    else:
        seconds, pause, write_seconds = 40, None, 0.003
    arrivals = []
    for index in range(int(seconds * fps)):
        due = index / fps
        if pause and pause[0] <= due < pause[1]:
            due = pause[1]
        arrivals.append((due, b"frame-%d" % index, index % gop == 0))
    bridge = make_bridge()
    bridge._pacer_fps = fps
    camera = SimulatedCamera(bridge, arrivals)
    monkeypatch.setattr(module.time, "monotonic", camera.monotonic)
    monkeypatch.setattr(module.time, "sleep", camera.sleep)
    writer = SimulatedWriter(camera, write_seconds)
    proc = SimpleNamespace(stdin=object(), poll=lambda: None)
    bridge._ffmpeg_proc = proc

    bridge._video_pacer(proc, writer, camera.buffer)

    assert bridge.diagnostics()["stream_dropped_frames"] == 0, (
        "VideoBuffer retired video the pacer could have written"
    )
    assert writer.written == [data for _, data, _ in arrivals]


def run_pacer_through_gap(monkeypatch, bridge, arrivals, events=()):
    bridge._pacer_fps = 15.0
    camera = SimulatedCamera(bridge, arrivals, events)
    monkeypatch.setattr(module.time, "monotonic", camera.monotonic)
    monkeypatch.setattr(module.time, "sleep", camera.sleep)
    writer = SimulatedWriter(camera, 0.0)
    proc = SimpleNamespace(stdin=object(), poll=lambda: None)
    bridge._ffmpeg_proc = proc
    bridge._video_pacer(proc, writer, camera.buffer)
    return writer


def test_video_gap_refeeds_only_the_held_idr_until_the_cameras_next_idr(monkeypatch):
    # Without data HA's stream worker fails after 30 s, and its reconnect
    # reports a timestamp discontinuity. The pacer re-feeds the last real IDR,
    # the only frame that decodes on its own. Afterwards nothing predicted
    # from before the gap may follow it: those frames refer to pictures the
    # re-fed IDR replaced, so video resumes at the camera's next IDR.
    fps = 15.0
    before = [(i / fps, b"before-%d" % i, i % 10 == 0) for i in range(15)]
    resume = 6.0
    after = [(resume + i / fps, b"after-%d" % i, i == 5) for i in range(10)]
    last_before, held = before[-1][0], before[10][1]
    bridge = make_bridge()
    during_gap = {}

    def sample_clocks():
        during_gap.update(
            last_real_video=bridge._last_real_video,
            last_video_write=bridge._last_video_write,
            video_write_started=bridge._video_write_started,
        )

    writer = run_pacer_through_gap(
        monkeypatch, bridge, before + after, [(resume - 0.5, sample_clocks)]
    )

    refeeds = len(writer.written) - len(before) - len(after[5:])
    assert refeeds >= 3
    assert writer.written == (
        [data for _, data, _ in before]
        + [held] * refeeds
        + [data for _, data, _ in after[5:]]
    ), "a frame other than the held IDR was re-fed, or predicted video followed it"
    times = writer.times[len(before):len(before) + refeeds]
    assert times[0] >= last_before + module._KEEPALIVE_GAP
    assert all(
        later - earlier >= module._KEEPALIVE_INTERVAL - 1e-9
        for earlier, later in zip(times, times[1:])
    )
    assert times[-1] < resume
    # Re-fed frames are not camera video: the receipt and mux-write clocks
    # still date from the last real frame, and nothing counts them.
    assert during_gap["last_real_video"] == last_before
    assert during_gap["last_video_write"] < times[0]
    assert during_gap["video_write_started"] == 0.0
    # Yet the stall watchdog watches each re-fed write (FFmpeg can wedge in a
    # gap too), and each restarts the bootstrap: a new HA client starts at
    # the latest re-fed IDR, not at a backlog dating from the last real one.
    assert writer.seen[len(before):len(before) + refeeds] == [
        (time, True) for time in times
    ]
    # The camera's predicted frames before its next IDR are the only drops.
    assert bridge.diagnostics()["stream_dropped_frames"] == 5


def test_camera_frame_queued_as_admission_closes_cancels_the_refeed(monkeypatch):
    # The camera's next frame can be queued after the pacer's get() timed
    # out, just before admission closes for a re-feed. VideoBuffer then
    # leaves admission open, so that frame and the rest of its GOP must
    # follow the last real video directly: a re-fed IDR ahead of them would
    # replace the pictures they refer to.
    fps = 15.0
    before = [(i / fps, b"before-%d" % i, i == 0) for i in range(15)]
    back = before[-1][0] + module._KEEPALIVE_GAP  # the first re-feed is due
    after = [(back + i / fps, b"after-%d" % i, False) for i in range(1, 6)]
    bridge = make_bridge()
    close_admission = module.VideoBuffer.require_keyframe_if_idle
    arrived = []

    def frame_lands_first(buffer):
        if not arrived:
            arrived.append(module.time.monotonic())
            # As on_video: the receipt clock, then admission.
            bridge._last_real_video = arrived[0]
            assert bridge._enqueue_video(b"after-0", reset_bootstrap=False)
        return close_admission(buffer)

    monkeypatch.setattr(module.VideoBuffer, "require_keyframe_if_idle", frame_lands_first)
    writer = run_pacer_through_gap(monkeypatch, bridge, before + after)

    assert arrived == [pytest.approx(back)]
    assert writer.written == (
        [data for _, data, _ in before] + [b"after-0"] + [data for _, data, _ in after]
    ), "the held IDR was re-fed ahead of the camera's frame"


def test_pacer_retired_during_a_gap_leaves_the_next_muxer_alone(monkeypatch):
    # A format change can retire this pacer's muxer while the pacer waits in
    # get(), which then returns straight into the re-feed. The stall clock
    # and the bootstrap reset belong to the next muxer by then: a stale stall
    # clock gets that healthy FFmpeg killed in its first gap, and a reset
    # cuts its bootstrap for new HA clients.
    fps = 15.0
    before = [(i / fps, b"before-%d" % i, i == 0) for i in range(15)]
    first_refeed = before[-1][0] + module._KEEPALIVE_GAP
    bridge = make_bridge()

    def start_the_next_muxer():
        bridge._ffmpeg_proc = Mock()

    writer = run_pacer_through_gap(
        monkeypatch, bridge, before + [(first_refeed + 5, b"after-idr", True)],
        [(first_refeed, start_the_next_muxer)],
    )

    assert writer.written == [data for _, data, _ in before]
    assert bridge._video_write_started == 0.0
    assert not bridge._bootstrap_reset.is_set()


def test_refed_idrs_stay_out_of_the_cameras_accounting(monkeypatch):
    # Auto's verdict, the pacer cadence, the drop counter and the gap clock
    # describe the camera. A held IDR written every interval must not hide
    # the very gap they measure. Real on_video and pacer thread; only the
    # FFmpeg process is replaced.
    monkeypatch.setattr(module, "_KEEPALIVE_GAP", 0.4, raising=False)
    monkeypatch.setattr(module, "_KEEPALIVE_INTERVAL", 0.1, raising=False)
    bridge = make_bridge()
    monkeypatch.setattr(bridge, "_wait_for_prewake", lambda: True)
    monkeypatch.setattr(bridge, "_should_keep_running", lambda: True)
    writes, pacers = [], []

    class Writer:
        def write(self, data):
            writes.append((module.time.monotonic(), data))

        def flush(self):
            pass

        def close(self):
            pass

    def start_process(codec):
        proc = Mock(stdout=None, stderr=None)
        proc.poll.side_effect = lambda: None if bridge._running else 0
        bridge._ffmpeg_proc = proc
        bridge._video_writer = Writer()
        bridge._video_queue = module.VideoBuffer()
        pacer = threading.Thread(
            target=bridge._video_pacer,
            args=(proc, bridge._video_writer, bridge._video_queue),
            daemon=True,
        )
        pacers.append(pacer)
        pacer.start()
        return True

    monkeypatch.setattr(bridge, "_start_ffmpeg_muxer_locked", start_process)
    keyframe = H264_SPS + H264_PPS + H264_IDR
    predicted = b"\x00\x00\x00\x01\x41\x9a\x21\x42"
    camera = {}

    def create_streamer(device, on_video, on_audio, on_login, on_disconnect,
                        video_id, manage_stream_switch=True):
        streamer = Mock(_cloudedge_native_signaling_retry=True)

        def deliver(frames):
            for frame in frames:
                camera.setdefault("first", module.time.monotonic())
                camera["last"] = module.time.monotonic()
                on_video(frame)
                module.time.sleep(1 / 15)

        def run():
            on_login()
            deliver([keyframe] + [predicted] * 4)
            camera["clock"] = bridge._last_real_video
            camera["gap_from"] = module.time.monotonic()
            module.time.sleep(1.5)  # the camera is silent
            camera["clock_after"] = bridge._last_real_video
            camera["write_clock_after"] = bridge._last_video_write
            camera["gap_to"] = module.time.monotonic()
            deliver([predicted] * 3 + [keyframe] + [predicted] * 2)
            module.time.sleep(0.2)  # the pacer writes the tail
            bridge._running = False
            return 11, 0

        streamer.run_session.side_effect = run
        return streamer

    bridge._coordinator.client.create_streamer = create_streamer
    try:
        bridge._stream_worker(1)
    finally:
        bridge._running = False
        for pacer in pacers:
            pacer.join(5)

    refeeds = [
        at for at, data in writes
        if camera["gap_from"] < at < camera["gap_to"] and data == keyframe
    ]
    assert len(refeeds) >= 3, "the pacer never bridged the gap"
    diagnostics = bridge.diagnostics()
    assert diagnostics["stream_last_frames"] == 11
    assert diagnostics["stream_last_max_gap"] >= 1.5
    assert diagnostics["stream_last_fps"] == pytest.approx(
        10 / (camera["last"] - camera["first"]), rel=0.05
    )
    # Only the camera's 3 predicted frames before its next IDR were refused.
    assert diagnostics["stream_dropped_frames"] == 3
    assert camera["clock_after"] == camera["clock"]
    assert camera["write_clock_after"] < refeeds[0]


@pytest.mark.parametrize("bound", ["max-hold", "bridge-stopped", "retry-cooldown"])
def test_held_idr_refeed_is_bounded(monkeypatch, bound):
    max_hold = 20.0
    monkeypatch.setattr(module, "_KEEPALIVE_MAX_HOLD", max_hold, raising=False)
    fps = 15.0
    before = [(i / fps, b"before-%d" % i, i == 0) for i in range(15)]
    last_before = before[-1][0]
    after = last_before + max_hold + 10
    bridge = make_bridge()
    limit, events = last_before + max_hold, []
    if bound != "max-hold":
        limit = 10.0

        def end_hold():
            if bound == "bridge-stopped":
                bridge._running = False
            else:  # the worker pauses the camera after repeated failures
                bridge._failure_cooldown_until = limit + 600

        events.append((limit, end_hold))

    writer = run_pacer_through_gap(
        monkeypatch, bridge, before + [(after, b"after-idr", True)], events
    )

    refeeds = [
        time for time, data in zip(writer.times, writer.written)
        if data == before[0][1] and time > last_before
    ]
    # Re-fed right up to the bound, never after it.
    assert refeeds, "the gap was never bridged"
    assert limit - 1.5 < refeeds[-1] <= limit
    # A stopped bridge's pacer exits; otherwise the camera's video still flows.
    assert (writer.written[-1] == b"after-idr") is (bound != "bridge-stopped")


@pytest.mark.parametrize("owner", ["live-muxer", "retired-by-bridge", "bridge-stopped"])
def test_muxer_that_exits_by_itself_reopens_the_keyframe_gate(monkeypatch, caplog, owner):
    # Only the stall watchdog re-armed the gate, and it needs a live process.
    # After a crash or an OOM kill the pacer and reader exit with FFmpeg while
    # on_video keeps queueing for it: no keyframe ever started a replacement.
    bridge = make_bridge()
    proc = Mock(returncode=-9)
    # The live muxer dies while watched. The others are already dead at the
    # first poll: the guard under the muxer lock decides, not the check after
    # the keepalive's sleep.
    exited = [owner != "live-muxer"]
    proc.poll.side_effect = lambda: -9 if exited[0] else None
    monkeypatch.setattr(module.time, "sleep", lambda _: exited.__setitem__(0, True))
    bridge._ffmpeg_proc = Mock() if owner == "retired-by-bridge" else proc
    bridge._running = owner != "bridge-stopped"
    bridge._got_keyframe = True
    assert bridge._enqueue_video(H264_SPS + H264_PPS + H264_IDR, reset_bootstrap=True)
    bridge._audio_buffer.extend(b"\xff" * 160)
    bridge._video_write_started = module.time.monotonic()  # far from a stall

    with caplog.at_level("WARNING", logger=module.__name__):
        bridge._video_keepalive(proc)

    # The bridge retires its own muxers before killing them, and a keepalive
    # that saw an exit can wait on the muxer lock while on_video replaces the
    # dead process: neither may touch the new muxer's queue. A stopped bridge
    # restarts nothing.
    restarted = owner == "live-muxer"
    assert bridge._got_keyframe is not restarted
    assert bridge._video_queue.empty() is restarted
    assert (not bridge._audio_buffer) is restarted
    assert (bridge._video_write_started == 0.0) is restarted
    assert ("exited unexpectedly" in caplog.text) is restarted


class ExitingMuxers:
    """FFmpeg processes that cannot run (a missing codec or demuxer), each
    watched by the bridge's own keepalive thread. A process exits when the
    camera script lets it: after the callback that started it returned."""

    def __init__(self, monkeypatch, bridge):
        self.bridge = bridge
        self.exits = []
        self.watchers = []
        monkeypatch.setattr(bridge, "_start_ffmpeg_muxer_locked", self.start)

    def start(self, codec):
        exited = threading.Event()
        proc = Mock(stdin=None, stdout=None, stderr=None, returncode=1)
        proc.poll.side_effect = lambda: 1 if exited.wait(5) else None
        self.bridge._ffmpeg_proc = proc
        self.bridge._video_queue = module.VideoBuffer(max_age=float("inf"))
        watcher = threading.Thread(
            target=self.bridge._video_keepalive, args=(proc,), daemon=True
        )
        self.exits.append(exited)
        self.watchers.append(watcher)
        watcher.start()
        return True

    def exit_newest(self):
        """The newest process exits; return once its watcher handled it."""
        self.exits[-1].set()
        self.watchers[-1].join(5)
        assert not self.watchers[-1].is_alive()

    def close(self):
        for exited in self.exits:
            exited.set()
        for watcher in self.watchers:
            watcher.join(5)


def test_ffmpeg_that_cannot_run_is_not_restarted_at_every_idr(monkeypatch, caplog):
    # The camera's next IDR replaces an FFmpeg that died. One that exits right
    # after every start was then started again at each IDR, with a warning
    # each time, for the whole session. A few quick exits in a row end the
    # retries until the camera's next session, which tries again.
    bridge = make_bridge()
    monkeypatch.setattr(bridge, "_wait_for_prewake", lambda: True)
    monkeypatch.setattr(bridge, "_should_keep_running", lambda: True)
    monkeypatch.setattr(module, "_RECONNECT_COOLDOWN", 0)
    monkeypatch.setattr(CloudEdgeStreamBridge, "client_count", property(lambda self: 1))
    muxers = ExitingMuxers(monkeypatch, bridge)
    keyframe = H264_SPS + H264_PPS + H264_IDR
    idrs = 8
    starts = []  # muxer starts in each camera session

    def create_streamer(device, on_video, on_audio, on_login, on_disconnect,
                        video_id, manage_stream_switch=True):
        streamer = Mock(_cloudedge_native_signaling_retry=True)

        def run():
            on_login()
            before = len(muxers.exits)
            if not starts:  # the first session: every FFmpeg exits at once
                for _ in range(idrs):
                    on_video(keyframe)
                    if len(muxers.exits) > before and not muxers.exits[-1].is_set():
                        muxers.exit_newest()
                    on_video(H264_P)
            else:  # the next one starts a muxer again
                on_video(keyframe)
                bridge._running = False
            starts.append(len(muxers.exits) - before)
            return 2, 0

        streamer.run_session.side_effect = run
        return streamer

    bridge._coordinator.client.create_streamer = create_streamer
    try:
        with caplog.at_level("WARNING", logger=module.__name__):
            bridge._stream_worker(1)
    finally:
        muxers.close()

    assert 1 < starts[0] < idrs, "a muxer was started at every IDR, or never retried"
    assert starts[1:] == [1], "the camera's next session did not try again"
    exits = [record.levelname for record in caplog.records if "exited" in record.getMessage()]
    assert exits == ["WARNING"] * (starts[0] - 1) + ["ERROR"]


@pytest.mark.parametrize("pacer", ["blocked-in-write", "waiting-for-video"])
def test_mux_stall_watchdog_kills_only_a_muxer_that_stopped_reading(
    monkeypatch, caplog, pacer
):
    # A wedged FFmpeg stays alive but stops reading stdin: the pacer blocks
    # inside its write and every later frame waits behind it. Past
    # _MUX_STALL_TIMEOUT the watchdog kills it, drops the input queued for it
    # and reopens the keyframe gate, so the camera's next IDR starts a new
    # muxer. A pacer that is only waiting for the camera (a video gap) is no
    # stall, however long the gap.
    timeout = module._MUX_STALL_TIMEOUT
    started = 1000.0
    bridge = make_bridge()

    def on_sleep(now):
        if now - started > 3 * timeout:
            bridge._running = False  # the viewer leaves; the watch ends

    clock = manual_clock(monkeypatch, started, on_sleep)
    killed_at = []
    proc = Mock()
    proc.poll.side_effect = lambda: -9 if killed_at else None
    proc.kill.side_effect = lambda: killed_at.append(clock[0])
    bridge._ffmpeg_proc = proc
    bridge._got_keyframe = True
    assert bridge._enqueue_video(H264_SPS + H264_PPS + H264_IDR, reset_bootstrap=True)
    bridge._audio_buffer.extend(b"\xff" * 160)
    bridge._last_video_write = started - 30.0  # nothing written for a while
    stalled = pacer == "blocked-in-write"
    if stalled:
        bridge._video_write_started = started  # the write never returns

    with caplog.at_level("WARNING", logger=module.__name__):
        bridge._video_keepalive(proc)

    if stalled:
        assert len(killed_at) == 1, "the wedged FFmpeg was never killed"
        assert timeout < killed_at[0] - started < 2 * timeout
        assert bridge._video_write_started == 0.0
        assert bridge._last_video_write == 0.0
    else:
        assert killed_at == [], "a muxer waiting for video was killed"
    assert bridge._got_keyframe is not stalled
    assert (bridge._video_queue.qsize() == 0) is stalled
    assert (not bridge._audio_buffer) is stalled
    assert ("stopped consuming input" in caplog.text) is stalled
