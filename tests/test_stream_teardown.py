"""Regressions for stream teardown and diagnostics responsiveness (F01/F05).

Both tests use real local processes/sockets with deterministic blocking
primitives, and guarantee cleanup in `finally` so no child process or
socket survives a failure.
"""
import socket
import subprocess
import threading
import time
from unittest.mock import Mock

import pytest

pytest.importorskip("homeassistant")

from custom_components.cloudedge.stream_bridge import CloudEdgeStreamBridge


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

    def sendall(self, data, flags=0):
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
        with bridge._stream_clients_lock:
            bridge._stream_clients.append(client)

        broadcaster = threading.Thread(target=broadcast, daemon=True)
        broadcaster.start()
        # Give the broadcaster time to enter the blocked sendall.
        time.sleep(0.2)
        assert not broadcast_done.is_set(), "expected the writer to be blocked"

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
        with bridge._stream_clients_lock:
            bridge._stream_clients.extend([client, good, dead])
        dead.close()  # already closed peer socket

        client.release()
        bridge._broadcast_stream(b"data")

        with bridge._stream_clients_lock:
            # The live client (released) and the good one survive in order;
            # only the dead socket is dropped.
            assert bridge._stream_clients == [client, good]
    finally:
        for sock in (client, peer, good, good_peer):
            try:
                sock.close()
            except OSError:
                pass
        bridge.stop("manager_stop")
