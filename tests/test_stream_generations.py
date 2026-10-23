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
