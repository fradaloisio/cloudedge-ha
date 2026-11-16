"""Retired SDK workers must not alter the next viewing session."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("homeassistant")

from custom_components.cloudedge.stream_bridge import CloudEdgeStreamBridge


H264_KEYFRAME = bytes.fromhex(
    "000000016742001e0000000168ce06e200000001658884"
)


class SessionHarness:
    """Replace the camera SDK, leaving bridge start/stop and callbacks real."""

    def __init__(self, monkeypatch):
        self.created = []
        self.workers = []
        self.controls = []
        self.release = [threading.Event(), threading.Event()]
        self.entered = [threading.Event(), threading.Event()]
        self.device_writes = []
        self.first_session = lambda on_video: self.release[0].wait(5)

        def set_device_config(serial_number, parameters, **kwargs):
            self.device_writes.append(dict(parameters))
            return True

        def create_streamer(device, on_video, on_audio, on_login, on_disconnect,
                            video_id, manage_stream_switch=True):
            index = len(self.created)
            streamer = Mock(_cloudedge_native_signaling_retry=True)
            self.created.append(streamer)

            def run():
                self.entered[index].set()
                if index == 0:
                    self.first_session(on_video)
                else:
                    assert self.release[index].wait(5)
                return 0, 0

            streamer.run_session.side_effect = run
            return streamer

        client = SimpleNamespace(
            set_device_config=set_device_config,
            create_streamer=create_streamer,
        )
        coordinator = SimpleNamespace(
            client=client,
            get_device_for_stream=lambda _: {
                "serial_number": "sn", "device_id": "id", "type_id": 1,
            },
            notify_stream_state_changed=lambda: None,
        )
        self.bridge = CloudEdgeStreamBridge(coordinator, "sn")
        self.bridge._stream_server = Mock()
        monkeypatch.setattr(self.bridge, "_kick_prewake", lambda: None)
        monkeypatch.setattr(self.bridge, "_wait_for_prewake", lambda: True)
        monkeypatch.setattr(self.bridge, "_should_keep_running", lambda: True)
        monkeypatch.setattr(self.bridge, "_idle_watch_loop", lambda _: None)

    def start(self):
        assert self.bridge._ensure_pipeline_started()
        self.workers.append(self.bridge._worker_thread)

    def in_thread(self, callback, name=None):
        thread = threading.Thread(target=callback, name=name)
        self.controls.append(thread)
        thread.start()
        return thread

    def close(self):
        for event in self.release:
            event.set()
        for thread in self.controls:
            thread.join(5)
        self.bridge.stop("manager_stop")
        for thread in self.workers:
            thread.join(5)
        assert not any(thread.is_alive() for thread in self.controls + self.workers)
        # Viewing must never change camera settings (IoT 167 is the front
        # light), whatever the stop/start interleaving.
        assert self.device_writes == []


class ContendedLock:
    """The bridge lock, reporting each thread that has to wait for it."""

    def __init__(self, lock, on_wait):
        self._lock = lock
        self._on_wait = on_wait

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            self._on_wait(threading.current_thread())
            self._lock.acquire()
        return self

    def __exit__(self, *exc_info):
        self._lock.release()


@pytest.fixture
def sessions(monkeypatch):
    harness = SessionHarness(monkeypatch)
    try:
        yield harness
    finally:
        harness.close()


def test_callback_paused_during_mux_start_cannot_publish_into_next_session(
    sessions, monkeypatch
):
    bridge = sessions.bridge
    # Set once stop() waits for the bridge lock behind the in-flight callback
    # (a repaired bridge) or, if it never has to, once stop() returned and the
    # next session started (a broken one).
    stop_settled = threading.Event()

    def on_wait(thread):
        if thread.name == "restarter":
            stop_settled.set()

    monkeypatch.setattr(bridge, "_lock", ContendedLock(bridge._lock, on_wait))
    before_mux = threading.Event()
    mux_generations = []
    real_start = bridge._start_ffmpeg_muxer

    def paused_start(*args, **kwargs):
        before_mux.set()
        assert sessions.release[0].wait(5)
        return real_start(*args, **kwargs)

    def start_process(codec):
        # The process boundary does not supply lifecycle decisions. Record
        # which viewing session receives the bridge's actual mux admission.
        mux_generations.append(bridge._session_generation)
        return True

    monkeypatch.setattr(bridge, "_start_ffmpeg_muxer", paused_start)
    monkeypatch.setattr(bridge, "_start_ffmpeg_muxer_locked", start_process)
    sessions.first_session = lambda on_video: on_video(H264_KEYFRAME)
    sessions.start()
    assert before_mux.wait(5)

    stop_returned = []

    def stop_and_view_again():
        # One thread, so the next view always follows the stop.
        bridge.stop("worker_exit")
        stop_returned.append(sessions.release[0].is_set())
        sessions.start()
        stop_settled.set()

    restarter = sessions.in_thread(stop_and_view_again, name="restarter")
    assert stop_settled.wait(5)
    sessions.release[0].set()
    restarter.join(5)
    assert not restarter.is_alive()
    assert sessions.entered[1].wait(5)
    sessions.workers[0].join(5)
    assert not sessions.workers[0].is_alive()

    new_generation = bridge._session_generation
    assert new_generation not in mux_generations
    assert not bridge._got_keyframe
    assert bridge._video_codec is None
    assert bridge._video_queue.empty()
    assert stop_returned == [True], "stop() returned while a media callback ran"


def test_retired_session_that_keeps_streaming_is_asked_to_stop_again(sessions):
    # pycloudedge <= 0.1.12 drops a stop requested before run_session()
    # starts: the retired session would keep the camera streaming, every
    # frame discarded (C06). Its next callback repeats the request.
    bridge = sessions.bridge
    stopped = threading.Event()
    delivered = threading.Event()

    def retired_but_streaming(on_video):
        assert stopped.wait(5)
        on_video(H264_KEYFRAME)
        delivered.set()
        assert sessions.release[0].wait(5)

    sessions.first_session = retired_but_streaming
    sessions.start()
    assert sessions.entered[0].wait(5)
    streamer = sessions.created[0]
    bridge.stop("worker_exit")
    assert streamer.request_stop.call_count == 1
    stopped.set()
    assert delivered.wait(5)

    assert streamer.request_stop.call_count == 2
    assert not bridge._got_keyframe, "the frame itself is discarded"


def test_live_session_gets_a_copy_of_the_coordinator_device(sessions, monkeypatch):
    # The library refreshes streaming metadata into the device dict it is
    # given, from the stream worker: the coordinator's own dict must not
    # change outside the event loop (C22).
    bridge = sessions.bridge
    device = {"serial_number": "sn", "device_id": "id", "type_id": 1}
    monkeypatch.setattr(bridge._coordinator, "get_device_for_stream", lambda _: device)
    create = bridge._coordinator.client.create_streamer
    received = []

    def create_and_refresh(device, **kwargs):
        received.append(device)
        device["host_key"] = "refreshed"
        return create(device=device, **kwargs)

    monkeypatch.setattr(bridge._coordinator.client, "create_streamer", create_and_refresh)
    sessions.start()
    assert sessions.entered[0].wait(5)

    assert received[0]["serial_number"] == "sn"
    assert device == {"serial_number": "sn", "device_id": "id", "type_id": 1}


def test_late_callback_of_a_retired_attempt_leaves_the_next_one_alone(monkeypatch):
    # A library that calls back from its own threads could deliver a retired
    # attempt's frame after the next attempt started: the repeated stop must
    # reach the retired session only (review).
    from test_stream_options import WatchedCamera, wait_until
    from custom_components.cloudedge.stream_bridge import StreamOptions

    camera = WatchedCamera(monkeypatch, StreamOptions())
    try:
        assert camera.view()
        late_audio = camera.sessions[0]["on_audio"]
        camera.bridge.set_stream_profile("SD")  # retires the first attempt
        wait_until(
            lambda: len(camera.streamers) == 2 and camera.streamers[1].streaming,
            "the next attempt never started",
        )

        late_audio(b"\xff" * 160)

        assert camera.streamers[0].request_stop.call_count == 2
        assert not camera.streamers[1].request_stop.called
    finally:
        camera.close()
