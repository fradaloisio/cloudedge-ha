"""Socket-boundary checks for slow viewers, bootstrap ordering and teardown."""

import socket
import threading
import time

import pytest

from custom_components.cloudedge import stream_consumers as module
from custom_components.cloudedge.stream_consumers import StreamConsumer


def test_slow_viewer_does_not_delay_another_viewer():
    slow, slow_peer = socket.socketpair()
    fast, fast_peer = socket.socketpair()
    slow.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    fast_peer.settimeout(0.5)
    closed = [threading.Event(), threading.Event()]
    consumers = [
        StreamConsumer(slow, lambda _: closed[0].set()),
        StreamConsumer(fast, lambda _: closed[1].set()),
    ]
    try:
        assert consumers[0].enqueue(b"x" * 262144)
        consumers[0].start()
        consumers[1].start()
        assert consumers[1].enqueue(b"live-frame")
        assert fast_peer.recv(1024) == b"live-frame"
        assert not closed[0].is_set(), "The fast viewer waited for the slow timeout"
    finally:
        for consumer in consumers:
            consumer.close()
        for peer in (slow_peer, fast_peer):
            peer.close()
        assert all(event.wait(3) for event in closed)


def test_bootstrap_and_concurrent_live_bytes_stay_ordered():
    writer, reader = socket.socketpair()
    writer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    reader.settimeout(3)
    closed = threading.Event()
    consumer = StreamConsumer(writer, lambda _: closed.set())
    bootstrap = b"header" * 16384
    live = b"live" * 4096
    try:
        assert consumer.enqueue(bootstrap)
        consumer.start()
        # The bootstrap is larger than the send buffer and has not been read.
        assert consumer.enqueue(live)
        received = bytearray()
        while len(received) < len(bootstrap) + len(live):
            chunk = reader.recv(65536)
            assert chunk, "The writer disconnected before delivering its stream"
            received.extend(chunk)
        assert received == bootstrap + live
    finally:
        consumer.close()
        reader.close()
        assert closed.wait(3)


def test_finish_sends_the_queued_chunks_then_the_last_one_without_waiting():
    # The bridge ends HA's source under its own locks and on HA's event loop:
    # finish() must not wait for a writer stuck on a full socket, and the
    # client must still read every queued chunk, then the end of the response.
    writer, reader = socket.socketpair()
    writer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    reader.settimeout(3)
    closed = threading.Event()
    consumer = StreamConsumer(writer, lambda _: closed.set(), chunked=True)
    block = b"x" * 65536
    try:
        assert consumer.enqueue(b"")  # an empty bootstrap: no chunk at all
        assert consumer.enqueue(block)
        consumer.start()
        started = time.monotonic()
        consumer.finish()
        assert time.monotonic() - started < 0.5, "finish() waited for the socket"
        assert not consumer.enqueue(b"late"), "accepted bytes after the end"
        received = bytearray()
        while chunk := reader.recv(65536):
            received.extend(chunk)
        assert received == b"10000\r\n" + block + b"\r\n" + b"0\r\n\r\n"
        assert closed.wait(3)
    finally:
        consumer.close()
        reader.close()


@pytest.mark.parametrize("overload", ["bytes", "age"])
def test_excessive_backlog_disconnects_and_releases_the_writer(monkeypatch, overload):
    writer, reader = socket.socketpair()
    reader.settimeout(1)
    now = [100.0]
    monkeypatch.setattr(module, "monotonic", lambda: now[0])
    closed = threading.Event()
    notifications = []

    def on_closed(sock):
        notifications.append(sock)
        closed.set()

    consumer = StreamConsumer(writer, on_closed)
    try:
        assert consumer.enqueue(b"bootstrap")
        if overload == "bytes":
            data = b"x" * (5 * 1024 * 1024)
        else:
            now[0] += 2.0
            data = b"next-frame"
        assert not consumer.enqueue(data)
        assert consumer.closed
        assert reader.recv(1) == b""
        # A close before start still results in exactly one writer callback.
        consumer.start()
        consumer.start()
        assert closed.wait(3)
        consumer.close()
        assert notifications == [writer]
        assert not consumer.enqueue(b"late-frame")
    finally:
        consumer.close()
        reader.close()


def test_a_viewer_that_keeps_up_outlasts_the_backlog_budget(monkeypatch):
    # The budget bounds what is still queued, not what was ever sent: every
    # delivered chunk gives its bytes back. A viewer reading each chunk as it
    # comes receives half again the whole budget and stays connected.
    monkeypatch.setattr(module, "monotonic", lambda: 100.0)  # only bytes count
    writer, reader = socket.socketpair()
    reader.settimeout(3)
    closed = threading.Event()
    consumer = StreamConsumer(writer, lambda _: closed.set())
    chunk_size = 64 * 1024
    chunks = module._MAX_PENDING_BYTES * 3 // 2 // chunk_size
    try:
        consumer.start()
        for index in range(chunks):
            chunk = bytes([index % 256]) * chunk_size
            accepted = consumer.enqueue(chunk)
            assert accepted, (
                f"disconnected after {index * chunk_size} bytes, all of them read"
            )
            received = bytearray()
            while len(received) < chunk_size:
                data = reader.recv(chunk_size - len(received))
                assert data, "The writer disconnected a viewer that keeps up"
                received.extend(data)
            assert received == chunk
        assert not consumer.closed
        # Every byte was delivered, so the whole budget is free again. A
        # partial release still lets the volume above through. The single
        # writer released each chunk before it sent the marker, so once the
        # marker arrives at most the marker's own byte is still counted.
        assert consumer.enqueue(b"!")
        assert reader.recv(1) == b"!"
        whole_budget = consumer.enqueue(bytes(module._MAX_PENDING_BYTES - 1))
        assert whole_budget, "delivered bytes still count against the backlog budget"
    finally:
        consumer.close()
        reader.close()
        assert closed.wait(3)
