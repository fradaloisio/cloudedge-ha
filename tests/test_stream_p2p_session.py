"""The bridge driving pycloudedge's own P2P session; only the network is scripted.

The other stream tests replace the SDK's streamer. Here the client's real
create_streamer() returns pycloudedge's P2PStreamer, which talks to a scripted
msgsvr (TCP) and to a scripted TURN server with the camera behind it (UDP),
all on 127.0.0.1. How long a library session lives, how it ends on request and
how it ends when a battery camera's live window closes are then exercised by
this integration's CI, on the pinned release and on a candidate library
(PYTHONPATH) alike. The scripts follow the wire formats, never the library's
own helpers.
"""
from __future__ import annotations

import base64
import json
import os
import select
import socket
import struct
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("cloudedge")
requests = pytest.importorskip("requests")

from Crypto.Cipher import DES3  # noqa: E402  (pycloudedge's own dependency)
from cloudedge import CloudEdgeClient  # noqa: E402
from cloudedge.p2p import p2p_streamer  # noqa: E402

from custom_components.cloudedge import stream_bridge as module  # noqa: E402
from custom_components.cloudedge.stream_bridge import CloudEdgeStreamBridge  # noqa: E402
from custom_components.cloudedge.stream_video_buffer import VideoBuffer  # noqa: E402

# A v79 battery camera: Auto opens the Android default (100), HD 102.
DEVICE = {
    "serial_number": "ABCD12345678",
    "device_id": 4242,
    "name": "Camera",
    "host_key": "0123456789abcdef0123456789abcdef",
    "type_id": 5,
    "capability_version": 79,
    "bps2": {"0": "640x360", "2": "2304x1296"},
}
AUTO_ID, HD_ID = 100, 102

# msgsvr frame: E6 node method cmd type E6 len_lo len_hi|encrypted, 3DES-ECB
# JSON, checksum, tail 9D.
MSGSVR_KEY = b"__#!HIRCloud5.0!#__".ljust(24, b"\x00")
REGISTER, WEBRTC, STATUS, HEARTBEAT = 0xC1, 0xC6, 0xC7, 0xC9

# STUN and TURN (RFC 5389, RFC 5766).
COOKIE = 0x2112A442
BINDING_REQUEST, BINDING_SUCCESS = 0x0001, 0x0101
ALLOCATE, ALLOCATE_SUCCESS, ALLOCATE_ERROR = 0x0003, 0x0103, 0x0113
REFRESH, REFRESH_SUCCESS = 0x0004, 0x0104
CREATE_PERMISSION, CREATE_PERMISSION_SUCCESS = 0x0008, 0x0108
CHANNEL_BIND, CHANNEL_BIND_SUCCESS = 0x0009, 0x0109
SEND_INDICATION, DATA_INDICATION = 0x0016, 0x0017
ERROR_CODE, CHANNEL_NUMBER, LIFETIME = 0x0009, 0x000C, 0x000D
XOR_PEER_ADDRESS, DATA, REALM, NONCE = 0x0012, 0x0013, 0x0014, 0x0015
XOR_RELAYED_ADDRESS, XOR_MAPPED_ADDRESS = 0x0016, 0x0020
MESSAGE_INTEGRITY = 0x0008

# KCP segment: conv cmd frg wnd ts sn una len, little endian; Meari conv 12.
KCP_HEADER = struct.Struct("<IBBHIIII")
KCP_CONV, KCP_PUSH, KCP_ACK, KCP_MSS = 0x0C, 81, 82, 1176
# IVA frame around each KCP message: FF 01, two session ids, flags, a marker.
IVA_HANDSHAKE, IVA_DATA = 0x7012, 0x7010
# VVP command packet: big-endian magic and command, video id at 0x38.
VVP_MAGIC, VVP_START_LIVE, VVP_STOP = 0x56565099, 0x11FF, 0x12FF
# Meari stream frame: 00 00 01 and its type, the payload length, the access
# unit; 128 bytes from 0x30 (I frame) or 0x28 (P frame) are 3DES-ECB encrypted.
STREAM_KEY = b"!mearicloud2.0!".ljust(24, b"\x00")

FPS, GOP = 15.0, 15
SPS = bytes.fromhex("000000016742001e")
PPS = bytes.fromhex("0000000168ce06e2")


def access_unit(session: int, index: int) -> bytes:
    """H.264 access unit `index` of a camera session; each one is unique."""
    filler = bytes([1 + (50 * session + index) % 250])
    if index % GOP == 0:  # an IDR, spread over three KCP fragments
        return SPS + PPS + b"\x00\x00\x00\x01\x65\x88\x84" + filler * 3000
    return b"\x00\x00\x00\x01\x41\x9a" + filler * 400


def stream_frame(unit: bytes, keyframe: bool) -> bytes:
    header_size, length_at, start = (0x3C, 0x38, 0x30) if keyframe else (0x34, 0x30, 0x28)
    frame = bytearray(header_size) + unit
    frame[:4] = b"\x00\x00\x01" + (b"\xfc" if keyframe else b"\xfd")
    struct.pack_into("<I", frame, length_at, len(unit))
    frame[start:start + 128] = DES3.new(STREAM_KEY, DES3.MODE_ECB).encrypt(
        bytes(frame[start:start + 128])
    )
    return bytes(frame)


def iva(marker: int, data: bytes) -> bytes:
    header = struct.pack("<BBHIIHHI", 0xFF, 0x01, 0, 0x0CAFE01, 0x0CAFE02, 0, marker, len(data))
    return header + data


def stun_message(msg_type: int, txn: bytes, attributes=()) -> bytes:
    body = b"".join(
        struct.pack(">HH", kind, len(value)) + value + b"\x00" * (-len(value) % 4)
        for kind, value in attributes
    )
    return struct.pack(">HHI", msg_type, len(body), COOKIE) + txn + body


def parse_stun(data: bytes):
    if len(data) < 20 or data[0] & 0xC0 or struct.unpack_from(">I", data, 4)[0] != COOKIE:
        return None
    msg_type, size = struct.unpack_from(">HH", data)
    attributes, offset = {}, 20
    while offset + 4 <= min(len(data), 20 + size):
        kind, length = struct.unpack_from(">HH", data, offset)
        attributes.setdefault(kind, data[offset + 4:offset + 4 + length])
        offset += 4 + length + (-length % 4)
    return msg_type, data[8:20], attributes


def xor_address(ip: str, port: int) -> bytes:
    cookie = struct.pack(">I", COOKIE)
    xored = bytes(a ^ b for a, b in zip(socket.inet_aton(ip), cookie))
    return struct.pack(">BBH", 0, 1, port ^ COOKIE >> 16) + xored


def parse_xor_address(value: bytes):
    if len(value) < 8:
        return None
    cookie = struct.pack(">I", COOKIE)
    ip = socket.inet_ntoa(bytes(a ^ b for a, b in zip(value[4:8], cookie)))
    return ip, struct.unpack_from(">H", value, 2)[0] ^ COOKIE >> 16


class LibraryClock:
    """The time module as the P2P library sees it, real time by default.

    Installed only where a test fast-forwards a camera's silence: the session
    timers then run `speed` times faster than real time.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._speed = 1.0
        self._real_anchor = time.monotonic()
        self._anchor = self._real_anchor
        self._wall_offset = time.time() - self._real_anchor

    def set_speed(self, speed: float) -> None:
        with self._lock:
            now = time.monotonic()
            self._anchor += (now - self._real_anchor) * self._speed
            self._real_anchor, self._speed = now, speed

    def monotonic(self) -> float:
        with self._lock:
            return self._anchor + (time.monotonic() - self._real_anchor) * self._speed

    def time(self) -> float:
        return self._wall_offset + self.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds) / self._speed)

    def __getattr__(self, name):
        return getattr(time, name)


class CameraSession:
    """What the camera saw of one viewer's TURN allocation."""

    def __init__(self, index: int, viewer, frames: int | None) -> None:
        self.index = index
        self.viewer = viewer
        self.frames_left = frames  # None: until the viewer stops it
        self.video_ids: list[int] = []  # each VVP START_LIVE
        self.stops = 0  # each VVP STOP
        self.sent: list[bytes] = []  # access units, in order
        self.last_frame_at: float | None = None  # library clock
        self.streaming = False
        self.silent = False  # the live window is over: the camera sleeps
        self.answered = False
        self.next_frame_at = 0.0
        self.send_sn = 0
        self.recv_sn = 0
        self.pending: dict[int, tuple[int, bytes]] = {}
        self.fragments: list[bytes] = []


class Camera:
    """A TURN server on 127.0.0.1 with the camera behind its relay.

    The camera offers only its relay candidate, so the whole session runs
    through this server: ICE checks, the KCP bootstrap and the VVP login come
    in as TURN data, and the camera answers with its IVA handshake, a login
    reply and encrypted stream frames carrying H.264, FPS per second. Camera
    session `i` sends `windows[i]` frames, if given, then falls silent like a
    battery camera at the end of its live window.
    """

    ADDRESS = ("127.0.0.1", 47000)  # the camera's relay candidate

    def __init__(self, clock: LibraryClock) -> None:
        self._clock = clock
        self.windows: list[int] = []
        self.sessions: list[CameraSession] = []
        self.datagrams = 0  # everything any viewer sent to this server
        self.errors: list[BaseException] = []  # the script's own failures
        self._by_viewer: dict[tuple[str, int], CameraSession] = {}
        self._channels: dict[tuple[str, int], dict[int, tuple[str, int]]] = {}
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._closing = False
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closing = True
        self._thread.join(5)
        self._sock.close()

    def _run(self) -> None:
        while not self._closing:
            try:
                readable, _, _ = select.select([self._sock], [], [], 0.005)
                if readable:
                    data, viewer = self._sock.recvfrom(65536)
                    self.datagrams += 1
                    self._from_network(data, viewer)
                self._send_due_frames()
            except Exception as err:  # reported by the test thread
                self.errors.append(err)
                return

    def _from_network(self, data: bytes, viewer) -> None:
        if len(data) >= 4 and 0x40 <= data[0] < 0x80:  # TURN ChannelData
            channel, size = struct.unpack_from(">HH", data)
            if self._channels.get(viewer, {}).get(channel) == self.ADDRESS:
                self._from_viewer(viewer, data[4:4 + size])
            return
        message = parse_stun(data)
        if message is None:
            return
        msg_type, txn, attributes = message
        reply = None
        if msg_type == BINDING_REQUEST:
            reply = stun_message(BINDING_SUCCESS, txn, [(XOR_MAPPED_ADDRESS, xor_address(*viewer))])
        elif msg_type == ALLOCATE and MESSAGE_INTEGRITY not in attributes:
            reply = stun_message(ALLOCATE_ERROR, txn, [
                (ERROR_CODE, b"\x00\x00\x04\x01Unauthorized"),
                (REALM, b"hangzhou"), (NONCE, b"test-nonce"),
            ])
        elif msg_type == ALLOCATE:
            self._channels[viewer] = {}
            reply = stun_message(ALLOCATE_SUCCESS, txn, [
                (XOR_RELAYED_ADDRESS, xor_address("127.0.0.1", 49000 + len(self._channels))),
                (XOR_MAPPED_ADDRESS, xor_address(*viewer)),
                (LIFETIME, struct.pack(">I", 600)),
            ])
        elif msg_type == CREATE_PERMISSION:
            reply = stun_message(CREATE_PERMISSION_SUCCESS, txn)
        elif msg_type == CHANNEL_BIND:
            channel = struct.unpack_from(">H", attributes[CHANNEL_NUMBER])[0]
            peer = parse_xor_address(attributes[XOR_PEER_ADDRESS])
            self._channels.setdefault(viewer, {})[channel] = peer
            reply = stun_message(CHANNEL_BIND_SUCCESS, txn)
        elif msg_type == REFRESH:
            reply = stun_message(REFRESH_SUCCESS, txn, [(LIFETIME, struct.pack(">I", 600))])
        elif msg_type == SEND_INDICATION:
            if parse_xor_address(attributes.get(XOR_PEER_ADDRESS, b"")) == self.ADDRESS:
                self._from_viewer(viewer, attributes.get(DATA, b""))
        if reply is not None:
            self._sock.sendto(reply, viewer)

    def _to_viewer(self, viewer, data: bytes) -> None:
        channel = next(
            (number for number, peer in self._channels.get(viewer, {}).items()
             if peer == self.ADDRESS),
            None,
        )
        if channel is None:
            frame = stun_message(DATA_INDICATION, os.urandom(12), [
                (XOR_PEER_ADDRESS, xor_address(*self.ADDRESS)), (DATA, data),
            ])
        else:
            frame = struct.pack(">HH", channel, len(data)) + data
            frame += b"\x00" * (-len(frame) % 4)
        self._sock.sendto(frame, viewer)

    def _from_viewer(self, viewer, data: bytes) -> None:
        session = self._by_viewer.get(viewer)
        if session is None:
            index = len(self.sessions)
            frames = self.windows[index] if index < len(self.windows) else None
            session = self._by_viewer[viewer] = CameraSession(index, viewer, frames)
            self.sessions.append(session)
        if session.silent:
            return
        stun = parse_stun(data)
        if stun is not None:
            if stun[0] == BINDING_REQUEST:  # an ICE check
                self._to_viewer(viewer, stun_message(BINDING_SUCCESS, stun[1]))
            return
        offset = 0
        while len(data) - offset >= KCP_HEADER.size:
            conv, cmd, frg, _, ts, sn, _, size = KCP_HEADER.unpack_from(data, offset)
            payload = data[offset + KCP_HEADER.size:offset + KCP_HEADER.size + size]
            offset += KCP_HEADER.size + size
            if conv != KCP_CONV or cmd != KCP_PUSH:
                continue
            if sn >= session.recv_sn:
                session.pending.setdefault(sn, (frg, payload))
            while session.recv_sn in session.pending:
                frg, chunk = session.pending.pop(session.recv_sn)
                session.recv_sn += 1
                session.fragments.append(chunk)
                if frg == 0:
                    self._message(session, b"".join(session.fragments))
                    session.fragments = []
            self._to_viewer(viewer, KCP_HEADER.pack(
                KCP_CONV, KCP_ACK, 0, 128, ts, sn, session.recv_sn, 0
            ))

    def _message(self, session: CameraSession, message: bytes) -> None:
        if len(message) < 20 or message[:2] != b"\xff\x01":
            return
        if struct.unpack_from("<H", message, 14)[0] != IVA_DATA:
            return  # the viewer's IVA handshake
        vvp = message[20:]
        if len(vvp) < 60 or struct.unpack_from(">I", vvp)[0] != VVP_MAGIC:
            return
        command = struct.unpack_from(">I", vvp, 12)[0]
        if command == VVP_START_LIVE:
            session.video_ids.append(vvp[0x38])
            if not session.answered:
                session.answered = True
                self._send(session, iva(IVA_HANDSHAKE, b""))
                self._send(session, iva(IVA_DATA, vvp[:60]))  # the login reply
            if not session.streaming:
                session.streaming = True
                session.next_frame_at = time.monotonic()
        elif command == VVP_STOP:
            session.stops += 1
            session.streaming = False

    def _send(self, session: CameraSession, message: bytes) -> None:
        chunks = [message[at:at + KCP_MSS] for at in range(0, len(message), KCP_MSS)]
        for position, chunk in enumerate(chunks):
            self._to_viewer(session.viewer, KCP_HEADER.pack(
                KCP_CONV, KCP_PUSH, len(chunks) - 1 - position, 128,
                int(time.monotonic() * 1000) & 0xFFFFFFFF,
                session.send_sn, session.recv_sn, len(chunk),
            ) + chunk)
            session.send_sn += 1

    def _send_due_frames(self) -> None:
        now = time.monotonic()
        for session in self.sessions:
            if not session.streaming or session.silent or now < session.next_frame_at:
                continue
            index = len(session.sent)
            unit = access_unit(session.index, index)
            session.sent.append(unit)
            self._send(session, iva(IVA_DATA, stream_frame(unit, index % GOP == 0)))
            session.last_frame_at = self._clock.monotonic()
            session.next_frame_at += 1 / FPS
            if session.frames_left is not None:
                session.frames_left -= 1
                session.silent = session.frames_left == 0


def msgsvr_frame(cmd: int, message: dict) -> bytes:
    payload = json.dumps(message, separators=(",", ":")).encode()
    padding = 8 - len(payload) % 8
    payload = DES3.new(MSGSVR_KEY, DES3.MODE_ECB).encrypt(payload + bytes([padding]) * padding)
    size = len(payload)
    header = bytes([0xE6, 0xA1, 0xB1, cmd, 0xD3, 0xE6, size & 0xFF, (size >> 8) & 0x7F | 0x80])
    return header + payload + bytes([sum(payload) & 0xFF, 0x9D])


def read_exact(conn: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise ConnectionError("the viewer closed the signaling connection")
        data += chunk
    return data


def read_msgsvr_frame(conn: socket.socket):
    header = read_exact(conn, 8)
    if header[0] != 0xE6 or header[5] != 0xE6:
        raise ValueError(f"not a msgsvr frame: {header.hex()}")
    size = header[6] | (header[7] & 0x7F) << 8
    payload = read_exact(conn, size + 2)[:size]
    if header[7] & 0x80:
        payload = DES3.new(MSGSVR_KEY, DES3.MODE_ECB).decrypt(payload)
        payload = payload[:-payload[-1]]
    return header[3], json.loads(payload)


class Msgsvr:
    """The signaling server: registration, camera status, TURN credentials
    and the camera's SDP answer. One record per viewer connection."""

    def __init__(self, clock: LibraryClock, turn_port: int) -> None:
        self._clock = clock
        self._turn_port = turn_port
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        self.connections: list[SimpleNamespace] = []
        self.errors: list[BaseException] = []  # the script's own failures
        self._sockets: list[socket.socket] = []
        self._threads = [threading.Thread(target=self._accept, daemon=True)]
        self._threads[0].start()

    def close(self) -> None:
        self._server.close()
        for conn in self._sockets:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for thread in self._threads:
            thread.join(5)

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            record = SimpleNamespace(commands=[], logout_at=None, closed=threading.Event())
            self.connections.append(record)
            self._sockets.append(conn)
            thread = threading.Thread(target=self._serve, args=(conn, record), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _serve(self, conn: socket.socket, record) -> None:
        try:
            while True:
                cmd, message = read_msgsvr_frame(conn)
                for reply in self._answer(record, cmd, message):
                    conn.sendall(msgsvr_frame(*reply))
        except OSError:  # the viewer left
            pass
        except Exception as err:  # reported by the test thread
            self.errors.append(err)
        finally:
            conn.close()
            record.closed.set()

    def _webrtc(self, content: dict):
        return WEBRTC, {
            "from": {
                "node": "webrtcsvr", "domain": "webrtcsvr.test", "transport": "tcp",
                "type": "binary", "ip": "127.0.0.1", "port": self.port,
            },
            "content": base64.b64encode(json.dumps(content).encode()).decode(),
        }

    def _answer(self, record, cmd: int, message: dict):
        if cmd == REGISTER:
            record.commands.append("register")
            return [(REGISTER, {
                "result": "OK", "uuid": "viewer-uuid", "token": "viewer-token",
                "hb": 30, "ht": 3,
            })]
        if cmd == STATUS:
            record.commands.append("status")
            return [(STATUS, {
                "action": "status", "uuid": message.get("uuid"), "status": "online",
                "contact": {}, "nat": {},
            })]
        if cmd == HEARTBEAT:  # the registration keepalive
            record.commands.append("heartbeat")
            return [(HEARTBEAT, {"result": "OK"})]
        if cmd != WEBRTC:
            record.commands.append(f"0x{cmd:02x}")
            return []
        request = json.loads(base64.b64decode(message.get("content", "")))
        kind = request.get("cmd")
        record.commands.append(kind)
        if kind == "hello":
            return [self._webrtc({"cmd": "hello", "tag": "tag"})]
        if kind == "option":
            return [self._webrtc({
                "cmd": "option", "coturn_ip": "127.0.0.1", "coturn_port": self._turn_port,
                "username": "1700000000:mearicloud", "pwd": "turn-password",
            })]
        if kind == "offer":
            ip, port = Camera.ADDRESS
            sdp = (
                "v=0\na=ice-ufrag:camera\na=ice-pwd:camera-password-0000\n"
                f"m=audio {port} RTP/AVP 0\nc=IN IP4 {ip}\n"
            )
            # The answer, then the end of the camera's (absent) trickle.
            completed = {"cmd": "nego", "state": "completed"}
            return [
                self._webrtc({"cmd": "answer", "sdp": sdp}),
                self._webrtc({"cmd": "candidate", "candidate": completed}),
            ]
        if kind == "candidate":
            return [self._webrtc({"cmd": "candidate", "result": "ok"})]
        if kind == "logout":
            record.logout_at = self._clock.monotonic()
            return [self._webrtc({"cmd": "logout", "result": "ok"})]
        return []


def refuse_http(*args, **kwargs):
    raise requests.ConnectionError("no network in this test")


class LiveView:
    """The bridge with pycloudedge's client and streamer, the scripted
    network and an HA stream worker's TCP connection; FFmpeg is the only
    other stand-in (each muxer start, and the frames meant for it)."""

    def __init__(self, tmp_path, monkeypatch) -> None:
        self.clock = LibraryClock()
        self.camera = Camera(self.clock)
        self.msgsvr = Msgsvr(self.clock, self.camera.port)
        self._monkeypatch = monkeypatch
        # The edges the library reaches: root discovery (UDP), the host's own
        # address for the SDP offer, and any HTTPS request.
        monkeypatch.setattr(
            p2p_streamer, "discover_msgsvr_endpoints",
            lambda **hints: [("127.0.0.1", self.msgsvr.port)],
        )
        monkeypatch.setenv("CLOUDEDGE_P2P_ADVERTISE_IP", "127.0.0.1")
        monkeypatch.setattr(requests.Session, "request", refuse_http)
        client = CloudEdgeClient(
            "viewer@example.test", "password", "IT", "+39",
            session_cache_file=str(tmp_path / "session"),
        )
        client.session_data = {"userID": 1234567, "mqtt": {}}
        client.OPENAPI_BASE_URL = "https://openapi.example.invalid"
        # The cloud's device listing, which names the camera unchanged.
        client.refresh_streaming_metadata = lambda device: device
        self.source_changes: list[str] = []
        coordinator = SimpleNamespace(
            client=client,
            get_device_for_stream=lambda serial: dict(DEVICE, bps2=dict(DEVICE["bps2"])),
            notify_stream_state_changed=lambda: None,
            set_runtime_connection_status=lambda serial, status: None,
            notify_stream_source_changed=lambda serial, close: (
                self.source_changes.append(serial), close(), True
            )[-1],
        )
        self.bridge = bridge = CloudEdgeStreamBridge(coordinator, DEVICE["serial_number"])
        monkeypatch.setattr(bridge, "_kick_prewake", lambda: None)
        monkeypatch.setattr(bridge, "_wait_for_prewake", lambda: True)
        self.muxer_starts: list[str] = []
        self.muxer_input: list[bytes] = []
        admit = bridge._enqueue_video

        def start_muxer(codec):
            self.muxer_starts.append(codec)
            bridge._ffmpeg_proc = Mock(stdin=None, stdout=None, stderr=None)
            bridge._ffmpeg_proc.poll.return_value = None
            bridge._video_queue = VideoBuffer(max_age=float("inf"))
            return True

        def enqueue(data, **kwargs):
            self.muxer_input.append(data)
            return admit(data, **kwargs)

        monkeypatch.setattr(bridge, "_start_ffmpeg_muxer_locked", start_muxer)
        monkeypatch.setattr(bridge, "_enqueue_video", enqueue)
        self.viewer: socket.socket | None = None

    def open_view(self) -> None:
        """HA's stream worker opens the bridge's source: viewing starts."""
        source = urlsplit(self.bridge.ensure_started())
        self.viewer = socket.create_connection((source.hostname, source.port))
        self.viewer.sendall(
            f"GET {source.path}?{source.query} HTTP/1.1\r\n"
            f"Host: {source.netloc}\r\n\r\n".encode()
        )

    def fast_forward_library(self) -> LibraryClock:
        if getattr(p2p_streamer, "time", None) is not time:
            pytest.fail(
                "pycloudedge's P2P session no longer reads its clocks through "
                "the time module: adapt LibraryClock to fast-forward it"
            )
        self._monkeypatch.setattr(p2p_streamer, "time", self.clock)
        return self.clock

    def close(self) -> None:
        worker = self.bridge.stop("manager_stop")
        if worker is not None:
            worker.join(5)
        if self.viewer is not None:
            self.viewer.close()
        self.camera.close()
        self.msgsvr.close()


@pytest.fixture
def live(tmp_path, monkeypatch):
    view = LiveView(tmp_path, monkeypatch)
    try:
        yield view
    finally:
        view.close()
    assert not view.camera.errors + view.msgsvr.errors, "the network script failed"


def wait_for(predicate, message: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail(message)
        time.sleep(0.01)


def test_library_sessions_end_on_a_profile_switch_and_on_stop(live):
    bridge, camera, msgsvr = live.bridge, live.camera, live.msgsvr
    live.open_view()
    wait_for(lambda: len(live.muxer_input) >= 20, "no live video reached the muxer")
    first = camera.sessions[0]
    assert first.video_ids == [AUTO_ID]
    # Decrypted, reassembled from KCP fragments and in order, IDR first.
    assert live.muxer_input[:20] == first.sent[:20]
    assert bridge.diagnostics()["stream_state"] == "streaming"

    # A profile switch ends the library session it replaces: the camera's
    # stream is stopped and the signaling session logged out and closed
    # before the next one opens with the new stream id.
    bridge.set_stream_profile("HD")
    wait_for(lambda: len(camera.sessions) == 2 and camera.sessions[1].sent,
             "HD never opened a camera session")
    second = camera.sessions[1]
    assert second.video_ids == [HD_ID]
    wait_for(lambda: first.stops, "the replaced session never stopped the camera's stream",
             timeout=2.0)
    assert msgsvr.connections[0].closed.wait(2), "the replaced session kept msgsvr open"
    assert msgsvr.connections[0].logout_at is not None
    wait_for(lambda: second.sent[0] in live.muxer_input,
             "the HD session's video never reached the muxer")
    # Same format: HA's source carried on through the switch.
    assert live.muxer_starts == ["h264"]
    assert live.source_changes == []

    # Home Assistant stops the bridge (unload or shutdown): the session ends
    # promptly and nothing of it keeps talking to the camera or to msgsvr.
    stopped_at = time.monotonic()
    worker = bridge.stop("manager_stop")
    worker.join(5)
    assert not worker.is_alive(), "run_session() outlived the bridge's stop"
    assert time.monotonic() - stopped_at < 3.0
    wait_for(lambda: second.stops, "the stopped session never stopped the camera's stream",
             timeout=2.0)
    assert msgsvr.connections[1].closed.wait(2)
    assert msgsvr.connections[1].logout_at is not None
    time.sleep(0.2)
    heard = camera.datagrams
    time.sleep(0.5)
    assert camera.datagrams == heard, "the ended session still sends to the camera"
    assert (len(camera.sessions), len(msgsvr.connections)) == (2, 2)


def test_camera_going_quiet_ends_the_session_in_time_for_the_viewer(live, monkeypatch):
    # A battery camera streams for about two minutes, then sleeps without a
    # word. The library has to notice and end its session, so the bridge can
    # open the next window for the viewer still watching, while the bridge
    # keeps HA's stream alive with its held IDR. That hold must outlast the
    # library's stall detection plus the reconnect cooldown.
    budget = module._KEEPALIVE_MAX_HOLD - module._RECONNECT_COOLDOWN
    monkeypatch.setattr(module, "_RECONNECT_COOLDOWN", 0)
    clock = live.fast_forward_library()
    window = 20
    live.camera.windows = [window]
    live.open_view()
    wait_for(lambda: live.camera.sessions and live.camera.sessions[0].silent,
             "the camera's live window never ended")
    first = live.camera.sessions[0]
    clock.set_speed(10.0)  # the library's silent wait, ten times faster
    wait_for(lambda: len(live.camera.sessions) == 2,
             f"the library kept a silent session open beyond {budget:.0f} s",
             timeout=budget / 10.0 + 5.0)
    clock.set_speed(1.0)
    assert live.msgsvr.connections[0].closed.wait(2)
    assert live.msgsvr.connections[0].logout_at is not None
    quiet_for = live.msgsvr.connections[0].logout_at - first.last_frame_at
    assert quiet_for <= budget, (
        f"the library ended the silent session after {quiet_for:.0f} s: "
        "_KEEPALIVE_MAX_HOLD no longer covers it"
    )
    second = live.camera.sessions[1]
    wait_for(lambda: second.sent and second.sent[0] in live.muxer_input,
             "the next window's video never reached the muxer")
    diagnostics = live.bridge.diagnostics()
    assert diagnostics["stream_last_frames"] == window
    assert diagnostics["stream_reconnects"] == 1
    assert diagnostics["stream_state"] == "streaming"
    assert live.muxer_input[:window] == first.sent
