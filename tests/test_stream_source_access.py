"""Only Home Assistant's consumers may open a camera's live source (C35).

The bridge listens on 127.0.0.1, where any local process can connect. Its
first consumer wakes the battery camera and starts a P2P session, and every
consumer receives the camera's video and audio. The source URL that Home
Assistant gets therefore carries a secret, checked before any of that.

The source is an HTTP response read by FFmpeg (HA's stream worker) and by
Go's HTTP client (go2rtc); when the bridge ends it, both must read a
complete response.
"""
from __future__ import annotations

import http.client
import io
import logging
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("homeassistant")

from custom_components.cloudedge import stream_bridge as module
from custom_components.cloudedge.stream_bridge import CloudEdgeStreamBridge

TS_PACKET = b"\x47" + bytes(187)


@pytest.fixture
def start_bridge(monkeypatch):
    """Start a bridge's stream server; waking the camera and the P2P session
    are recorded instead of run. Patch module settings before calling it."""
    bridges = []

    def start() -> SimpleNamespace:
        bridge = CloudEdgeStreamBridge(
            SimpleNamespace(client=None, notify_stream_state_changed=lambda: None),
            "camera",
        )
        bridges.append(bridge)
        view = SimpleNamespace(bridge=bridge, wakes=[], sessions=[])
        monkeypatch.setattr(bridge, "_kick_prewake", lambda: view.wakes.append(True))
        monkeypatch.setattr(bridge, "_stream_worker", view.sessions.append)
        monkeypatch.setattr(bridge, "_idle_watch_loop", lambda gen: None)
        view.source = bridge.ensure_started()
        return view

    try:
        yield start
    finally:
        for bridge in bridges:
            bridge.stop("manager_stop")


def connect(source: str, request: bytes | None) -> socket.socket:
    url = urlsplit(source)
    sock = socket.create_connection((url.hostname, url.port), timeout=2)
    if request is not None:
        sock.sendall(request)
    return sock


def stream_request(source: str, version: str = "HTTP/1.1") -> bytes:
    """The request FFmpeg and go2rtc send for the source URL."""
    url = urlsplit(source)
    return (
        f"GET {url.path}?{url.query} {version}\r\nHost: {url.netloc}\r\n\r\n".encode()
    )


def read_answer(sock: socket.socket, until: bytes | None = None) -> bytes:
    """Read until EOF (or `until`); a client left waiting gets what it has."""
    data = b""
    while until is None or until not in data:
        try:
            chunk = sock.recv(65536)
        except (TimeoutError, ConnectionResetError):
            break
        if not chunk:
            break
        data += chunk
    return data


def wait_for(predicate, message: str) -> None:
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, message
        time.sleep(0.01)


def test_only_the_secret_source_url_wakes_the_camera_and_streams(
    monkeypatch, start_bridge
):
    monkeypatch.setattr(module, "_STREAM_REQUEST_TIMEOUT", 0.3, raising=False)
    view = start_bridge()
    bridge, source, wakes, sessions = view.bridge, view.source, view.wakes, view.sessions
    url = urlsplit(source)
    secret = parse_qs(url.query).get("auth", [""])[0]
    wrong = secret[:-1] + ("B" if secret.endswith("A") else "A")
    host = f"Host: {url.netloc}\r\n\r\n"
    refused = {
        "no secret": f"GET {url.path or '/'} HTTP/1.1\r\n{host}".encode(),
        "wrong secret": f"GET {url.path}?auth={wrong} HTTP/1.1\r\n{host}".encode(),
        # What the former tcp:// source served: connect, send nothing. The
        # 404 comes when the request timeout expires.
        "silent client": None,
    }
    sockets: list[socket.socket] = []
    try:
        for case, request in refused.items():
            sock = connect(source, request)
            sockets.append(sock)
            # The bridge decides before it answers and closes the connection.
            answer = read_answer(sock)
            assert not wakes and not sessions, f"{case}: the camera was woken"
            assert bridge.client_count == 0, f"{case}: registered as a consumer"
            assert not bridge.is_streaming, f"{case}: the pipeline started"
            assert answer.startswith(b"HTTP/1.1 404 "), f"{case}: {answer[:40]!r}"

        # The URL Home Assistant was given opens the stream, and keeps opening
        # it: HA's Stream and go2rtc reconnect to the source they cached.
        for connection in range(2):
            viewer = connect(bridge.ensure_started(), stream_request(source))
            sockets.append(viewer)
            header = read_answer(viewer, until=b"\r\n\r\n")
            assert header.startswith(b"HTTP/1.1 200 "), header[:40]
            assert b"\r\nContent-Type: video/mp2t\r\n" in header
            assert b"\r\nTransfer-Encoding: chunked\r\n" in header
            wait_for(lambda: bridge.client_count == 1 and sessions,
                     f"connection {connection} did not start the stream")
            bridge._broadcast_stream(TS_PACKET)
            chunk = b"bc\r\n" + TS_PACKET + b"\r\n"  # 188 bytes
            body = read_answer(viewer, until=chunk)
            assert body.endswith(chunk), f"connection {connection}: no media"
            viewer.close()
            wait_for(lambda: bridge.client_count == 0, "the closed viewer stayed registered")
        # One wake and one P2P session served both connections.
        assert (len(wakes), len(sessions)) == (1, 1)
        assert bridge.ensure_started() == source
    finally:
        for sock in sockets:
            sock.close()


def test_a_silent_local_client_holds_up_no_other_and_gets_its_404(
    monkeypatch, start_bridge
):
    # The accept loop read one request at a time: a local process that
    # connected and sent nothing held Home Assistant's own connection back
    # until its request timed out, and was then closed without the 404.
    monkeypatch.setattr(module, "_STREAM_REQUEST_TIMEOUT", 1.5)
    view = start_bridge()
    silent = connect(view.source, None)
    viewer = connect(view.source, stream_request(view.source))
    try:
        viewer.settimeout(0.75)  # half the request timeout
        header = read_answer(viewer, until=b"\r\n\r\n")
        assert header.startswith(b"HTTP/1.1 200 "), "the viewer waited for the silent client"
        wait_for(lambda: view.bridge.client_count == 1 and view.sessions,
                 "the viewer did not start the stream")
        silent.setblocking(False)
        with pytest.raises(BlockingIOError):
            silent.recv(1)  # still unanswered: its request may yet come
        silent.settimeout(3)
        assert read_answer(silent).startswith(b"HTTP/1.1 404 ")
        assert view.bridge.client_count == 1
    finally:
        silent.close()
        viewer.close()


def test_unanswered_connections_are_bounded(monkeypatch, start_bridge):
    # Each connection waits for its request on a thread of its own. Past the
    # bound a connection is closed at once, so local processes cannot pile up
    # threads, and an answered connection gives its place back.
    monkeypatch.setattr(module, "_STREAM_MAX_PENDING_REQUESTS", 1)
    monkeypatch.setattr(module, "_STREAM_REQUEST_TIMEOUT", 1.0)
    view = start_bridge()
    first = connect(view.source, None)
    second = connect(view.source, None)
    sockets = [first, second]
    try:
        second.settimeout(0.5)
        assert second.recv(1) == b"", "the connection beyond the bound was kept"
        first.settimeout(3)
        assert read_answer(first).startswith(b"HTTP/1.1 404 ")
        viewer = connect(view.source, stream_request(view.source))
        sockets.append(viewer)
        assert read_answer(viewer, until=b"\r\n\r\n").startswith(b"HTTP/1.1 200 ")
    finally:
        for sock in sockets:
            sock.close()


@pytest.mark.parametrize("version", ["HTTP/1.1", "HTTP/1.0"])
def test_a_source_the_bridge_ends_is_a_complete_http_response(start_bridge, version):
    """go2rtc reads the source with Go's HTTP client. That client takes chunks
    only from an HTTP/1.1 status line, and to it a chunked response without
    its last chunk is an unexpected EOF, as it is to http.client here. An
    HTTP/1.0 client must not get chunks: it reads until the connection ends.
    """
    view = start_bridge()
    media = [TS_PACKET * 2, bytes(range(256)) * 8, TS_PACKET]
    sock = connect(view.source, stream_request(view.source, version))
    response = http.client.HTTPResponse(sock)
    outcome = {}

    def read() -> None:
        try:
            outcome["body"] = response.read()
        except Exception as err:  # noqa: BLE001 - reported by the assertion
            outcome["error"] = err

    try:
        response.begin()
        assert (response.status, response.version) == (200, 11)
        assert response.getheader("Transfer-Encoding") == (
            "chunked" if version == "HTTP/1.1" else None
        )
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        wait_for(lambda: view.bridge.client_count == 1, "the client was not registered")
        for block in media:
            view.bridge._broadcast_stream(block)
        # What a format change does to the current source.
        view.bridge._close_stream_consumers()
        reader.join(5)
        assert outcome == {"body": b"".join(media)}
    finally:
        response.close()
        sock.close()


FRAMES = 20


def mpegts_video(frames: int) -> bytes:
    """A small MPEG-TS video stream, encoded with PyAV."""
    import av

    output = io.BytesIO()
    with av.open(output, "w", format="mpegts") as container:
        stream = container.add_stream("mpeg2video", rate=10)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        for index in range(frames):
            frame = av.VideoFrame(64, 48, "yuv420p")
            for plane in frame.planes:
                plane.update(bytes([index * 8 % 256]) * plane.buffer_size)
            frame.pts = index
            container.mux(stream.encode(frame))
        container.mux(stream.encode())
    return output.getvalue()


@pytest.mark.parametrize("end", ["format change", "stop"])
def test_home_assistant_reads_the_end_of_a_source_the_bridge_ends(
    monkeypatch, caplog, start_bridge, end
):
    """HA's stream worker reads the source with FFmpeg. Before, only the closed
    connection ended the response: FFmpeg logged "Stream ends prematurely"
    and raised an I/O error, which HA logged at ERROR on every format change
    and stop, FFmpeg's message included (PyAV appends it to the exception).
    """
    av = pytest.importorskip("av")
    # FFmpeg would send the request to a proxy (see the proxy test below).
    monkeypatch.delenv("http_proxy", raising=False)
    media = mpegts_video(FRAMES)
    view = start_bridge()
    outcome = {}

    def stream_worker() -> None:
        # The I/O of HA's stream worker: open the source, demux to its end.
        try:
            with av.open(view.source, timeout=10) as container:
                outcome["packets"] = sum(1 for packet in container.demux() if packet.size)
        except av.FFmpegError as err:
            outcome["error"] = err

    previous_level = av.logging.get_level()
    av.logging.set_level(av.logging.VERBOSE)  # as HA does at stream DEBUG level
    caplog.set_level(logging.WARNING, logger="libav")
    worker = threading.Thread(target=stream_worker, daemon=True)
    try:
        worker.start()
        wait_for(lambda: view.bridge.client_count == 1, "HA's worker never connected")
        view.bridge._broadcast_stream(media)
        if end == "format change":
            view.bridge._close_stream_consumers()
        else:
            view.bridge.stop("manager_stop")
        worker.join(15)
        assert not worker.is_alive(), "the worker never read the end of the stream"
    finally:
        av.logging.set_level(previous_level)
    errors = [
        f"[{record.name}] {record.getMessage()}"
        for record in caplog.records
        if record.name.startswith("libav") and record.levelno >= logging.ERROR
    ]
    assert errors == []
    assert outcome == {"packets": FRAMES}


PROXY = "http://ha:PROXY-PASSWORD@proxy.example:3128"


# What FFmpeg 8.0 (PyAV 16.0.1 and 17.0.1) does with a request for
# http://127.0.0.1 in each environment, observed against loopback listeners.
@pytest.mark.parametrize(
    ("environment", "proxied"),
    [
        ({"http_proxy": PROXY}, True),
        # It reads only the lowercase no_proxy, split at commas and spaces,
        # and compares host names: no localhost alias, no address ranges.
        ({"http_proxy": PROXY, "no_proxy": "localhost,127.0.0.0/8", "NO_PROXY": "127.0.0.1"}, True),
        ({"http_proxy": PROXY, "no_proxy": "localhost,127.0.0.1\n"}, True),
        ({"http_proxy": PROXY, "no_proxy": "localhost, 127.0.0.1"}, False),
        ({"http_proxy": PROXY, "no_proxy": "*"}, False),
        # It ignores HTTP_PROXY, and an http_proxy without http://.
        ({"HTTP_PROXY": PROXY}, False),
        ({"http_proxy": "proxy.example:3128"}, False),
    ],
)
def test_a_proxy_that_would_get_the_source_url_is_reported_once(
    monkeypatch, caplog, environment, proxied
):
    """Through a proxy HA's stream worker never reaches the bridge.

    It fails with an error that hides the URL, and the proxy receives the
    URL, secret included. Only go2rtc connects directly.
    """
    for name in ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    # Logged once per process: forget an earlier test's warning.
    monkeypatch.setattr(module, "_proxy_warning_logged", False, raising=False)
    caplog.set_level(logging.WARNING, logger=module.__name__)
    coordinator = SimpleNamespace(client=None, notify_stream_state_changed=lambda: None)
    bridges = [CloudEdgeStreamBridge(coordinator, camera) for camera in ("front", "garden")]
    try:
        sources = [bridge.ensure_started() for bridge in bridges]
    finally:
        for bridge in bridges:
            bridge.stop("manager_stop")

    warnings = [
        record.getMessage() for record in caplog.records
        if record.name == module.__name__ and record.levelno >= logging.WARNING
    ]
    assert len(warnings) == (1 if proxied else 0), warnings
    if proxied:
        assert "no_proxy" in warnings[0] and "127.0.0.1" in warnings[0]
    for source in sources:
        assert parse_qs(urlsplit(source).query)["auth"][0] not in caplog.text
    assert "PROXY-PASSWORD" not in caplog.text
