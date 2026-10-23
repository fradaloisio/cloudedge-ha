"""Regressions for stream teardown and diagnostics responsiveness (F01/F05).

The tests use real local processes/sockets with deterministic blocking
primitives, and guarantee cleanup in `finally` so no child process or
socket survives a failure.
"""
import os
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("homeassistant")

from custom_components.cloudedge import stream_bridge as module
from custom_components.cloudedge.stream_bridge import (
    CloudEdgeStreamBridge,
    CloudEdgeStreamManager,
)


def make_bridge():
    bridge = CloudEdgeStreamBridge(Mock(), "sn-test")
    bridge._coordinator.client = None  # diagnostics reads OPENAPI_BASE_URL
    bridge._coordinator.notify_stream_state_changed = Mock()
    return bridge


# ── F01: stop must not deadlock on a wedged process with a full pipe ──────

STOP_BUDGET_SECONDS = 15.0


def test_stop_completes_while_writer_is_blocked_on_full_pipe():
    """A process that never reads stdin, a writer thread blocked inside
    write(), and stop() must still finish within an explicit budget."""
    bridge = make_bridge()
    proc = subprocess.Popen(
        ["sleep", "30"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    bridge._running = True
    bridge._ffmpeg_proc = proc

    writer_blocked = threading.Event()
    writer_exited = threading.Event()

    def writer():
        try:
            # Fill the pipe (64KB on macOS) then stay blocked inside the
            # raw write syscall, exactly like the video pacer on a wedged
            # muxer.
            while True:
                proc.stdin.write(b"x" * 65536)
                proc.stdin.flush()
                writer_blocked.set()
        except (BrokenPipeError, OSError, ValueError):
            writer_exited.set()

    writer_thread = threading.Thread(target=writer, daemon=True)
    try:
        writer_thread.start()
        assert writer_blocked.wait(timeout=5)

        started = time.monotonic()
        bridge.stop("stopped")
        elapsed = time.monotonic() - started

        assert elapsed < STOP_BUDGET_SECONDS, (
            f"stop() took {elapsed:.1f}s — deadlocked on the pipe"
        )
        assert writer_exited.wait(timeout=5), "writer never released"
        assert proc.poll() is not None, "child process still alive"
        assert bridge._ffmpeg_proc is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def test_stop_is_idempotent_and_allows_restart():
    bridge = make_bridge()
    proc = subprocess.Popen(
        ["sleep", "30"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    bridge._running = True
    bridge._ffmpeg_proc = proc
    try:
        bridge.stop("stopped")
        # Second stop on the torn-down bridge returns immediately.
        started = time.monotonic()
        bridge.stop("stopped")
        assert time.monotonic() - started < 2.0
        # A subsequent start works.
        assert bridge.ensure_started() is not None
        assert bridge.stream_source is not None
    finally:
        bridge.stop("manager_stop")
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


# ── F05: diagnostics must not wait on a stalled transport writer ──────────

class BlockingSendSocket(socket.socket):
    """A real socket whose sendall blocks until released (deterministic)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._release = threading.Event()
        self.entered = threading.Event()

    def sendall(self, data, flags=0):
        self.entered.set()
        self._release.wait(timeout=30)
        return socket.socket.sendall(self, data, flags)

    def release(self):
        self._release.set()


def _make_blocking_client():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = BlockingSendSocket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(listener.getsockname())
    peer, _ = listener.accept()
    listener.close()
    return client, peer


def test_diagnostics_stay_responsive_while_a_client_writer_is_stalled():
    bridge = make_bridge()
    client, peer = _make_blocking_client()
    broadcast_done = threading.Event()

    def broadcast():
        bridge._broadcast_stream(b"chunk" * 1024)
        broadcast_done.set()

    try:
        bridge._register_stream_client(client)

        broadcaster = threading.Thread(target=broadcast, daemon=True)
        broadcaster.start()
        assert client.entered.wait(5), "expected the consumer writer to be blocked"
        assert broadcast_done.wait(1), "producer waited for a slow consumer"

        started = time.monotonic()
        assert bridge.client_count == 1
        diagnostics = bridge.diagnostics()
        elapsed = time.monotonic() - started

        assert elapsed < 5.0, (
            f"diagnostics took {elapsed:.1f}s — blocked behind the transport"
        )
        assert diagnostics["stream_clients"] == 1

        client.release()
        assert broadcast_done.wait(timeout=5)
        broadcaster.join(timeout=5)
        assert not broadcaster.is_alive()
    finally:
        client.release()
        try:
            client.close()
        except OSError:
            pass
        try:
            peer.close()
        except OSError:
            pass
        bridge.stop("manager_stop")


def test_broadcast_removes_dead_clients_without_losing_good_ones():
    bridge = make_bridge()
    client, peer = _make_blocking_client()
    good = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    good.connect(listener.getsockname())
    good_peer, _ = listener.accept()
    listener.close()

    try:
        dead = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        for sock in (client, good, dead):
            bridge._register_stream_client(sock)
        dead.close()  # already closed peer socket

        client.release()
        bridge._broadcast_stream(b"data")

        good_peer.settimeout(3)
        peer.settimeout(3)
        assert good_peer.recv(4) == b"data"
        assert peer.recv(4) == b"data"

        # The dead client's writer unregisters it, asynchronously. Read the
        # registry itself: client_count prunes closed sockets on its own and
        # would hide a writer that never unregistered its client.
        def registered():
            with bridge._stream_clients_lock:
                return set(bridge._stream_clients), set(bridge._stream_consumers)

        live = {client, good}
        deadline = time.monotonic() + 3
        while registered() != (live, live) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert registered() == (live, live)
    finally:
        for sock in (client, peer, good, good_peer):
            try:
                sock.close()
            except OSError:
                pass
        bridge.stop("manager_stop")


# ── C08: Home Assistant stop must leave no session or FIFO behind ─────────

def test_stop_all_waits_for_closing_sessions_but_not_for_a_stuck_one(monkeypatch):
    """HA exits right after stop_streams() and the bridge threads are daemons:
    each P2P session needs time to close (logout, sockets), yet one stuck in
    network I/O must not hold up the unload or the shutdown."""
    monkeypatch.setattr(module, "_STOP_JOIN_TIMEOUT", 1.0, raising=False)
    entered = {"closing": threading.Event(), "stuck": threading.Event()}
    closed = threading.Event()
    release = threading.Event()

    def create_streamer(device, on_video, on_audio, on_login, on_disconnect,
                        video_id, manage_stream_switch=True):
        serial = device["serial_number"]
        stop_requested = threading.Event()
        streamer = Mock(_cloudedge_native_signaling_retry=True)
        streamer.request_stop.side_effect = stop_requested.set

        def run():
            entered[serial].set()
            if serial == "stuck":
                release.wait(10)  # blocked in I/O, deaf to request_stop()
            else:
                stop_requested.wait(10)
                time.sleep(0.3)  # the SDK logs out and closes its sockets
                closed.set()
            return 0, 0

        streamer.run_session.side_effect = run
        return streamer

    coordinator = SimpleNamespace(
        client=SimpleNamespace(create_streamer=create_streamer),
        get_device_for_stream=lambda serial: {
            "serial_number": serial, "device_id": serial, "type_id": 1,
        },
        notify_stream_state_changed=lambda: None,
    )
    manager = CloudEdgeStreamManager(coordinator)
    workers = []
    try:
        for serial in entered:
            bridge = manager.get_bridge(serial)
            bridge._stream_server = Mock()  # the local listener HA opened
            monkeypatch.setattr(bridge, "_kick_prewake", lambda: None)
            monkeypatch.setattr(bridge, "_wait_for_prewake", lambda: True)
            monkeypatch.setattr(bridge, "_should_keep_running", lambda: True)
            monkeypatch.setattr(bridge, "_idle_watch_loop", lambda _: None)
            assert bridge._ensure_pipeline_started()
            workers.append(bridge._worker_thread)
        assert all(event.wait(5) for event in entered.values())

        started = time.monotonic()
        manager.stop_all()
        elapsed = time.monotonic() - started

        assert closed.is_set(), "stop_all() returned before the session closed"
        assert elapsed < 3.0, f"a stuck session held stop_all() for {elapsed:.1f}s"
    finally:
        release.set()
        manager.stop_all()
        for worker in workers:
            worker.join(5)
    assert not any(worker.is_alive() for worker in workers)


def test_audio_fifo_left_by_a_killed_process_does_not_disable_audio():
    """HA can die mid-stream (daemon threads never clean up). Its FIFO must not
    collide with the next process's: 'File exists' meant a muxer without audio."""
    script = (
        "import sys\n"
        "from types import SimpleNamespace\n"
        "from custom_components.cloudedge.stream_bridge import CloudEdgeStreamBridge\n"
        "print(CloudEdgeStreamBridge(SimpleNamespace(), sys.argv[1])._prepare_audio_fifo())\n"
    )
    serial = f"fifo-{uuid.uuid4().hex[:8]}"
    paths = []
    try:
        for _ in range(2):  # the same camera in two HA processes; no cleanup
            result = subprocess.run(
                [sys.executable, "-c", script, serial],
                cwd=Path(__file__).parents[1],
                capture_output=True, text=True, timeout=60, check=True,
            )
            paths.append(result.stdout.strip().splitlines()[-1])
        assert "None" not in paths, f"audio disabled: {result.stderr.strip()}"
        assert paths[0] != paths[1]
    finally:
        for path in paths:
            if path != "None":
                CloudEdgeStreamBridge._cleanup_audio_fifo(path)


def test_audio_fifo_directory_is_private_and_removed_with_the_fifo():
    bridge = make_bridge()
    first, second = bridge._prepare_audio_fifo(), bridge._prepare_audio_fifo()
    try:
        assert first and second
        for path in (first, second):
            assert stat.S_ISFIFO(os.stat(path).st_mode)
            # Other local users can neither open nor pre-create it.
            assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
        # Both cleanup paths: the audio writer once FFmpeg has exited, and stop().
        bridge._audio_writer(SimpleNamespace(poll=lambda: 0), first)
        bridge._running = True
        bridge._audio_fifo_path = second
        bridge.stop("manager_stop")
        leftovers = [
            entry
            for path in (first, second)
            for entry in (path, os.path.dirname(path))
            if os.path.lexists(entry)
        ]
    finally:
        for path in (first, second):
            CloudEdgeStreamBridge._cleanup_audio_fifo(path)
    assert leftovers == []


def test_audio_fifo_directory_is_removed_when_the_fifo_cannot_be_created(monkeypatch, tmp_path):
    """The muxer runs without audio, and its private directory is not leaked."""
    attempts = []

    def mkfifo(path, mode=0o666):
        attempts.append(path)
        raise OSError("FIFOs are not supported on this filesystem")

    monkeypatch.setattr(module.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(module.os, "mkfifo", mkfifo)

    assert make_bridge()._prepare_audio_fifo() is None
    # The FIFO was attempted inside a fresh private directory under tmp_path...
    assert [Path(path).parent.parent for path in attempts] == [tmp_path]
    # ...and that directory is gone.
    assert list(tmp_path.iterdir()) == []
