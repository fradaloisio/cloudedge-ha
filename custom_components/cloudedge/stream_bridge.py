"""Per-camera live stream bridge for CloudEdge battery cameras.

This module keeps the Home Assistant camera entity thin. The entity exposes a
``stream_source()`` URL, while this bridge owns the blocking pieces:

* CloudEdge P2P streaming via ``pycloudedge``
* H.264/HEVC bitstream inspection
* ``ffmpeg`` muxing raw video and G.711 audio into MPEG-TS
* a tiny local HTTP server that streams only to clients requesting its
  secret ``http://127.0.0.1:<port>/stream.ts?auth=<token>`` URL (Home
  Assistant's stream worker, go2rtc) and refuses any other local process
"""
from __future__ import annotations

import errno
import inspect
import logging
import os
import queue
import secrets
import select
import socket
import subprocess
import tempfile
import threading
import time
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .stream_profile_policy import (
    resolve_auto_stream_ids,
    resolve_stream_profile_ids,
)
from .stream_consumers import StreamConsumer
from .stream_muxer import TimestampedVideoWriter
from .stream_video_buffer import VideoBuffer

if TYPE_CHECKING:
    from . import CloudEdgeCoordinator

_LOGGER = logging.getLogger(__name__)

_IDLE_TIMEOUT = 45.0
# A full reconnect is now a last resort after pycloudedge has attempted an
# in-session STOP/START recovery. Cameras can rate-limit immediate
# re-establishment, so keep a short device cooldown between sessions.
_RECONNECT_COOLDOWN = 8.0
_MAX_CONSECUTIVE_STREAM_FAILURES = 4
# Home Assistant's stream worker reconnects its TCP input immediately after an
# error.  Keep the failure state across those new local clients so they cannot
# wake the battery camera and enable live mode forever. The pause starts at
# 60 s and doubles with every further attempt without video, up to 10 min;
# the first video frame or a stream profile change resets it.
_FAILED_SESSION_COOLDOWN = 60.0
_FAILED_SESSION_COOLDOWN_MAX = 600.0
_WAKE_RETRY_COOLDOWN = 15.0
_PREWAKE_RESULT_TTL = 30.0
_ONLINE_GRACE_WINDOW = 20.0
_SIGNAL_STATUS_RETRY_WINDOW = 8.0
_SIGNAL_STATUS_RETRY_POLL = 1.0
_MAX_MPEGTS_BOOTSTRAP_BYTES = 4 * 1024 * 1024
# A local consumer sends its HTTP request as soon as it connects (FFmpeg in
# HA's stream worker, go2rtc). Each connection is answered on its own thread,
# which waits this long at most for the request before it answers 404.
_STREAM_REQUEST_TIMEOUT = 2.0
_STREAM_REQUEST_MAX_BYTES = 4096
# At most this many connections wait for their request at the same time; a
# further one is closed at once, so local processes cannot pile up threads.
# A real consumer sends its request right away and is answered at once.
_STREAM_MAX_PENDING_REQUESTS = 8
# Chunked, so the bridge can end the stream with the last chunk. A response
# that only the closed connection ends reads to FFmpeg as "Stream ends
# prematurely" (an I/O error in HA's stream worker), and Go's HTTP client
# (go2rtc) reads chunks only from an HTTP/1.1 status line.
_STREAM_RESPONSE_OK = (
    b"HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\n"
    b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
)
# For an HTTP/1.0 request, which must not get chunks (RFC 9112).
_STREAM_RESPONSE_OK_UNCHUNKED = (
    b"HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\nConnection: close\r\n\r\n"
)
_STREAM_RESPONSE_NOT_FOUND = (
    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
)
_MUX_STALL_TIMEOUT = 10.0
# An FFmpeg that exits this soon after its start never got going (a missing
# codec or demuxer, bad arguments): each camera IDR would start another one.
# After _MUXER_QUICK_EXITS such exits in a row, no muxer starts until the
# next P2P attempt.
_MUXER_QUICK_EXIT = 5.0
_MUXER_QUICK_EXITS = 3
# During a video gap the pacer re-feeds the held IDR to keep HA's stream
# worker reading: first after _KEEPALIVE_GAP without camera video (a normal
# frame interval is at most 1/_PACER_MIN_FPS), then every _KEEPALIVE_INTERVAL.
_KEEPALIVE_GAP = 2.0
_KEEPALIVE_INTERVAL = 1.0
# The hold spans a battery camera's reconnect after its ~2 min live window:
# pycloudedge notices the stall after ~50 s, the bridge waits
# _RECONNECT_COOLDOWN (8 s), and the next window's first video may take up to
# the library's 30 s first-video timeout (~10 s observed). 50 + 8 + 30 = 88 s.
# Past the hold the camera is gone, and HA's stream reports it.
_KEEPALIVE_MAX_HOLD = 90.0
# Shared by every bridge when the integration stops: time for each P2P session
# to finish its own teardown before Home Assistant exits (the bridge threads
# are daemons and would simply vanish).
_STOP_JOIN_TIMEOUT = 5.0

# Camera audio: G.711 µ-law, 8 kHz mono (8000 bytes/s), transcoded to AAC.
# The writer feeds ffmpeg at a fixed cadence — real audio when available,
# µ-law silence otherwise — so ffmpeg never waits for its audio input. That
# does not keep the output flowing: during a video gap ffmpeg holds the audio
# back and releases it in a burst when video resumes. Only video (the re-fed
# IDR, see _video_pacer) carries the output through a gap.
_AUDIO_ENABLED = True
_AUDIO_CHUNK_BYTES = 160          # 20 ms @ 8 kHz
_AUDIO_CHUNK_SECONDS = 0.02
_AUDIO_BUFFER_MAX_BYTES = 16000   # cap buffered audio at 2 s (bounds latency)
_MULAW_SILENCE = 0xFF
_AUTO_FALLBACK_WINDOWS = 2
_AUTO_SUBSTREAM_FAILURES = 2
_AUTO_MAX_SUSTAINABLE_FPS = 6.0
_AUTO_MAX_FRAME_GAP = 2.5
_PACER_MIN_FPS = 2.0
_PACER_MAX_FPS = 15.0
_SUBSTREAM_INITIAL_FPS = 5.0

STREAM_PROFILE_AUTO = "Auto"
STREAM_PROFILE_MAIN = "HD"
STREAM_PROFILE_SUBSTREAM = "SD"
STREAM_PROFILE_OPTIONS = (
    STREAM_PROFILE_AUTO,
    STREAM_PROFILE_MAIN,
    STREAM_PROFILE_SUBSTREAM,
)
_STREAM_PROFILE_ALIASES = {
    "auto": STREAM_PROFILE_AUTO,
    STREAM_PROFILE_AUTO: STREAM_PROFILE_AUTO,
    "main": STREAM_PROFILE_MAIN,
    "hd": STREAM_PROFILE_MAIN,
    STREAM_PROFILE_MAIN: STREAM_PROFILE_MAIN,
    "substream": STREAM_PROFILE_SUBSTREAM,
    "sd": STREAM_PROFILE_SUBSTREAM,
    STREAM_PROFILE_SUBSTREAM: STREAM_PROFILE_SUBSTREAM,
}


def _failed_session_cooldown(failures: int) -> float:
    """Return the retry pause after `failures` attempts in a row without video."""
    doublings = min(10, max(0, failures - _MAX_CONSECUTIVE_STREAM_FAILURES))
    return min(_FAILED_SESSION_COOLDOWN_MAX, _FAILED_SESSION_COOLDOWN * 2 ** doublings)


# HA's stream worker opens the source with FFmpeg, which sends the request
# through the lowercase http_proxy variable (when it starts with http://)
# unless the lowercase no_proxy, split at commas and spaces, names the host:
# localhost or 127.0.0.0/8 does not cover 127.0.0.1. The worker then never
# reaches the bridge, and the proxy receives the URL with its secret.
# go2rtc connects directly.
_proxy_warning_logged = False


def _warn_if_source_is_proxied() -> None:
    """Warn once per process if HA's stream worker would use a proxy."""
    global _proxy_warning_logged
    if _proxy_warning_logged or not os.environ.get("http_proxy", "").startswith("http://"):
        return
    no_proxy = os.environ.get("no_proxy", "").replace(",", " ").split(" ")
    if "*" in no_proxy or "127.0.0.1" in no_proxy:
        return
    _proxy_warning_logged = True
    _LOGGER.warning(
        "The http_proxy environment variable is set and no_proxy does not list "
        "127.0.0.1: Home Assistant's stream worker (HLS live view, live still "
        "images, recordings) will send the cameras' local stream URL, secret "
        "included, to the proxy and fail. Add 127.0.0.1 to the lowercase "
        "no_proxy variable; the WebRTC live view is not affected"
    )


def normalize_stream_profile(profile: str) -> str:
    """Return the canonical stream profile label."""
    try:
        return _STREAM_PROFILE_ALIASES[profile]
    except KeyError as exc:
        raise ValueError(f"Unsupported stream profile: {profile}") from exc


def _convert_to_annex_b(frame_data: bytes) -> bytes:
    """Convert a single H.264/HEVC access unit to Annex B if needed."""
    if not frame_data:
        return frame_data

    if frame_data.startswith(b"\x00\x00\x00\x01") or frame_data.startswith(b"\x00\x00\x01"):
        return frame_data

    converted = bytearray()
    offset = 0
    remaining = len(frame_data)

    while remaining >= 4:
        nal_len = int.from_bytes(frame_data[offset:offset + 4], "big")
        next_offset = offset + 4 + nal_len
        if nal_len <= 0 or next_offset > len(frame_data):
            break
        converted.extend(b"\x00\x00\x00\x01")
        converted.extend(frame_data[offset + 4:next_offset])
        offset = next_offset
        remaining = len(frame_data) - offset

    if converted and offset == len(frame_data):
        return bytes(converted)

    return b"\x00\x00\x00\x01" + frame_data


def _detect_video_codec(nal_units: list[bytes]) -> str | None:
    """Detect H.264 or HEVC from parameter-set NAL units."""
    # H.264 P slices commonly begin with 0x41, which superficially looks like
    # HEVC type 32. HEVC headers have a second byte, layer zero and a nonzero
    # temporal id; validate those bits before interpreting parameter sets.
    if any(
        len(nal) >= 2 and not nal[0] & 0x81
        and nal[1] >> 3 == 0 and nal[1] & 7
        and ((nal[0] >> 1) & 0x3F) in (32, 33, 34)
        for nal in nal_units
    ):
        return "hevc"
    # H.264 only from its SPS. HEVC STSA_N, IDR_N_LP and EOS headers (0x08,
    # 0x28, 0x48) share the H.264 PPS type bits; a base-layer HEVC header is
    # even, so it can never read as an SPS (7).
    if any((nal[0] & 0x1F) == 7 for nal in nal_units):
        return "h264"
    return None


def _is_video_keyframe(nal_units: list[bytes], codec: str) -> bool:
    """Return True if the access unit contains an actual keyframe slice.

    Parameter sets (SPS/PPS/VPS) alone do NOT count: an IDR-less access
    unit cannot be decoded, and an IDR without parameter sets is handled
    separately by prepending cached ones.
    """
    if codec == "h264":
        return any((nal[0] & 0x1F) == 5 for nal in nal_units)
    return any(((nal[0] >> 1) & 0x3F) in (19, 20) for nal in nal_units)


def _split_nal_units(frame_data: bytes) -> list[bytes]:
    """Return each Annex B NAL unit payload (start code stripped).

    bytes.find locates the start codes, so splitting a large I-frame costs
    a few calls per NAL unit rather than a Python step per byte.
    """
    nal_units = []
    start = frame_data.find(b"\x00\x00\x01")
    while start >= 0:
        start += 3
        end = frame_data.find(b"\x00\x00\x01", start)
        if end < 0:
            payload = frame_data[start:]
        else:
            # Zeros before a start code belong to it (the leading zero of a
            # 4-byte start code, trailing_zero_8bits).
            payload = frame_data[start:end].rstrip(b"\x00")
        if payload:
            nal_units.append(payload)
        start = end
    return nal_units


def _extract_parameter_sets(frame_data: bytes, codec: str) -> bytes:
    """Return the SPS/PPS (H.264) or VPS/SPS/PPS (HEVC) NAL units."""
    out = bytearray()
    for nal in _split_nal_units(frame_data):
        if codec == "h264":
            is_ps = (nal[0] & 0x1F) in (7, 8)
        else:
            is_ps = ((nal[0] >> 1) & 0x3F) in (32, 33, 34)
        if is_ps:
            out += b"\x00\x00\x00\x01" + nal
    return bytes(out)


class CloudEdgeStreamBridge:
    """Bridge one CloudEdge camera to a local MPEG-TS HTTP endpoint."""

    def __init__(self, coordinator: CloudEdgeCoordinator, serial_number: str) -> None:
        self._coordinator = coordinator
        self._serial_number = serial_number

        self._lock = threading.Lock()
        self._running = False
        # Incremented on every pipeline start. Worker/idle threads capture
        # their generation and exit when it changes, so a thread blocked in
        # a long wait during stop() cannot resurrect into the next session.
        self._session_generation = 0
        self._attempt_generation = 0
        self._got_keyframe = False
        self._muxer_quick_exits = 0

        self._stream_port = 0
        # Only a request for this target gets the stream: any local process
        # can reach the port. It lasts as long as the bridge, so the source
        # that HA's Stream and go2rtc keep stays valid across reconnects.
        self._stream_target = f"/stream.ts?auth={secrets.token_urlsafe(32)}"
        self._stream_server: socket.socket | None = None
        self._stream_accept_thread: threading.Thread | None = None
        self._stream_clients: list[socket.socket] = []
        self._stream_consumers: dict[socket.socket, StreamConsumer] = {}
        self._stream_clients_lock = threading.Lock()
        self._mpegts_bootstrap = bytearray()
        self._bootstrap_reset = threading.Event()

        self._ffmpeg_proc: subprocess.Popen | None = None
        self._video_writer: TimestampedVideoWriter | None = None
        self._ffmpeg_lock = threading.Lock()
        self._video_codec: str | None = None
        self._muxer_parameter_sets: bytes | None = None
        self._muxer_first_keyframe = b""
        self._mux_epoch: float | None = None
        self._ffmpeg_reader_thread: threading.Thread | None = None
        self._ffmpeg_stderr_thread: threading.Thread | None = None
        self._video_pacer_thread: threading.Thread | None = None
        self._video_keepalive_thread: threading.Thread | None = None
        self._audio_writer_thread: threading.Thread | None = None
        self._audio_fifo_path: str | None = None
        self._audio_buffer = bytearray()
        self._audio_buffer_lock = threading.Lock()
        self._video_queue = VideoBuffer()

        self._worker_thread: threading.Thread | None = None
        self._idle_watch_thread: threading.Thread | None = None
        self._p2p_streamer = None

        self._stream_profile = STREAM_PROFILE_AUTO
        self._active_video_id: int | None = None
        # Auto's start ID and its degraded fallback (equal when the camera
        # has no strictly lighter profile than its default).
        self._main_video_id = 0
        self._substream_video_id = 1
        # Explicit HD/SD selections (largest / smallest advertised profile),
        # unknown until an attempt resolves the camera's profiles.
        self._hd_video_id: int | None = None
        self._sd_video_id: int | None = None
        self._available_video_ids: tuple[int, ...] = ()
        self._profile_changed = threading.Event()
        self._auto_degraded_windows = 0
        self._auto_substream_failures = 0

        self._metrics_lock = threading.Lock()
        self._stream_state = "idle"
        self._reconnect_count = 0
        self._wake_count = 0
        self._last_session_frames = 0
        self._last_session_bytes = 0
        self._last_session_duration = 0.0
        self._last_session_fps = 0.0
        self._last_max_frame_gap = 0.0
        self._pacer_fps = _PACER_MAX_FPS
        self._video_dropped_frames = 0
        self._audio_frames = 0
        self._audio_bytes = 0
        self._last_audio_at = 0.0
        self._last_error: str | None = None
        self._failure_cooldown_until = 0.0
        self._consecutive_stream_failures = 0
        # Attempt generation current when the bridge itself last stopped an
        # attempt (viewer gone / profile change). Such an attempt says
        # nothing about the camera. A second stop while the attempt is still
        # closing records a later generation, hence the >= in the worker.
        self._bridge_stopped_attempt = 0

        self._last_request_time = 0.0
        self._last_client_time = 0.0
        self._source_restart_until = 0.0
        self._last_video_write = 0.0
        self._video_write_started = 0.0
        self._last_real_video = 0.0
        self._last_stop_reason = "idle"
        self._last_wake_attempt = 0.0
        self._last_online_confirmation = 0.0
        self._prewake_lock = threading.Lock()
        self._prewake_thread: threading.Thread | None = None
        self._prewake_done = threading.Event()
        self._prewake_result: bool | None = None
        self._prewake_completed_at = 0.0

    @property
    def is_streaming(self) -> bool:
        """Return whether the bridge is currently active."""
        return self._running

    @property
    def client_count(self) -> int:
        """Return the number of connected local MPEG-TS clients."""
        with self._stream_clients_lock:
            self._prune_disconnected_clients_locked()
            return len(self._stream_clients)

    def _prune_disconnected_clients_locked(self) -> None:
        """Remove HA TCP consumers that closed before media was available."""
        dead: list[socket.socket] = []
        for client in self._stream_clients:
            try:
                readable, _, exceptional = select.select([client], [], [client], 0)
                if exceptional:
                    dead.append(client)
                elif readable and not client.recv(1, socket.MSG_PEEK):
                    dead.append(client)
            except (ConnectionError, OSError, ValueError):
                dead.append(client)
        for client in dead:
            self._stream_clients.remove(client)
            consumer = self._stream_consumers.pop(client, None)
            if consumer is not None:
                consumer.close()
            else:
                client.close()

    @property
    def stream_source(self) -> str | None:
        """Return the local MPEG-TS source URL, secret included."""
        if not self._stream_port:
            return None
        # As the auth query parameter, the secret is masked wherever Home
        # Assistant logs the URL (stream worker errors, go2rtc output).
        return f"http://127.0.0.1:{self._stream_port}{self._stream_target}"

    @property
    def stream_profile(self) -> str:
        """Return the requested stream profile."""
        return self._stream_profile

    def set_stream_profile(self, profile: str) -> None:
        """Select the stream profile and restart only the active P2P attempt."""
        profile = normalize_stream_profile(profile)
        with self._lock:
            if profile == self._stream_profile:
                return

            self._stream_profile = profile
            self._active_video_id = None
            self._auto_degraded_windows = 0
            self._auto_substream_failures = 0
            self._consecutive_stream_failures = 0
            self._failure_cooldown_until = 0.0
            self._profile_changed.set()
            self._bridge_stopped_attempt = self._attempt_generation
            self._attempt_generation += 1
            streamer = self._p2p_streamer
            if streamer is not None:
                try:
                    streamer.request_stop()
                except Exception:
                    pass
        self._coordinator.notify_stream_state_changed()

    def diagnostics(self) -> dict[str, object]:
        """Return bounded runtime diagnostics for the camera entity."""
        client = self._coordinator.client
        openapi_base = getattr(client, "OPENAPI_BASE_URL", "") if client else ""
        openapi_host = urlparse(openapi_base).hostname or None
        with self._metrics_lock:
            cooldown = max(0.0, self._failure_cooldown_until - time.monotonic())
            state = self._stream_state
            if state == "retry_cooldown" and not cooldown:
                state = "idle"  # the pause expired or a profile change reset it
            if self._active_video_id is None:
                active_profile = None  # no stream opened for this profile yet
            elif (
                self._active_video_id == self._sd_video_id
                and self._sd_video_id != self._hd_video_id
            ):
                active_profile = STREAM_PROFILE_SUBSTREAM
            else:
                active_profile = STREAM_PROFILE_MAIN
            return {
                "stream_profile": self._stream_profile,
                "stream_profile_active": active_profile,
                "stream_video_id": self._active_video_id,
                "stream_main_video_id": self._hd_video_id,
                "stream_substream_video_id": self._sd_video_id,
                "stream_available_video_ids": list(self._available_video_ids),
                "stream_state": state,
                "stream_clients": self.client_count,
                "stream_codec": self._video_codec,
                "stream_openapi_host": openapi_host,
                "stream_reconnects": self._reconnect_count,
                "stream_wakes": self._wake_count,
                "stream_last_frames": self._last_session_frames,
                "stream_last_bytes": self._last_session_bytes,
                "stream_last_duration": round(self._last_session_duration, 2),
                "stream_last_fps": round(self._last_session_fps, 2),
                "stream_last_max_gap": round(self._last_max_frame_gap, 2),
                "stream_pacer_fps": round(self._pacer_fps, 2),
                # VideoBuffer admission refusals only: frames discarded with
                # a retired GOP are not counted.
                "stream_dropped_frames": self._video_dropped_frames,
                "stream_audio_codec": "pcm_mulaw/8000",
                "stream_audio_frames": self._audio_frames,
                "stream_audio_bytes": self._audio_bytes,
                "stream_audio_active": bool(
                    self._last_audio_at
                    and time.monotonic() - self._last_audio_at < 5.0
                ),
                "stream_last_error": self._last_error,
                "stream_retry_cooldown": round(cooldown, 1),
            }

    def ensure_started(self) -> str | None:
        """Start the bridge if needed and return the local stream URL."""
        with self._lock:
            self._last_request_time = time.monotonic()
            already_up = self._stream_server is not None
            if not already_up and not self._start_stream_server():
                return None
        return self.stream_source

    def _kick_prewake(self) -> None:
        if self._running:
            return
        now = time.monotonic()
        if (now - self._last_online_confirmation) < _ONLINE_GRACE_WINDOW:
            return
        if (now - self._last_wake_attempt) < _WAKE_RETRY_COOLDOWN:
            return
        with self._prewake_lock:
            if self._prewake_thread is not None:
                if self._prewake_thread.is_alive():
                    return
                if now - self._prewake_completed_at < _PREWAKE_RESULT_TTL:
                    return
                self._prewake_thread = None
            self._prewake_result = None
            self._prewake_completed_at = 0.0
            self._prewake_done.clear()
            self._prewake_thread = threading.Thread(
                target=self._prewake_worker,
                name=f"cloudedge_prewake_{self._serial_number}",
                daemon=True,
            )
            self._prewake_thread.start()

    def _prewake_worker(self) -> None:
        result = False
        try:
            device = self._get_stream_device()
            if device is not None:
                result = self._wake_camera_if_needed(device)
        except Exception as err:
            _LOGGER.debug("Pre-wake failed for %s: %s", self._serial_number, err)
        finally:
            with self._prewake_lock:
                self._prewake_result = result
                self._prewake_completed_at = time.monotonic()
                self._prewake_done.set()

    def _wait_for_prewake(self) -> bool | None:
        """Wait for an in-flight pre-wake and return its result, if any."""
        with self._prewake_lock:
            thread = self._prewake_thread
        if thread is None:
            return None
        if not self._prewake_done.wait(timeout=22.0):
            return None
        with self._prewake_lock:
            if thread is not self._prewake_thread:
                return None
            result = self._prewake_result
            self._prewake_thread = None
            self._prewake_result = None
            self._prewake_completed_at = 0.0
        return result

    def stop(self, reason: str = "stopped") -> threading.Thread | None:
        """Stop the bridge and free all resources.

        Return the stopped session's worker thread: its P2P session may still
        be closing when this returns. _stop_locked() forgets the thread, so it
        is captured here under the same lock.
        """
        with self._lock:
            worker = self._worker_thread
            self._stop_locked(reason)
        return worker

    def _stop_locked(self, reason: str) -> None:
        if not (
            self._running
            or self._stream_server
            or self._ffmpeg_proc
            or self._worker_thread
            or self._stream_port
        ):
            return

        self._last_stop_reason = reason
        self._running = False
        self._session_generation += 1
        self._attempt_generation += 1
        with self._metrics_lock:
            self._stream_state = reason
        close_listener = reason in {"manager_stop", "device_removed", "stopped"}
        if self._p2p_streamer is not None:
            try:
                self._p2p_streamer.request_stop()
            except Exception:
                pass

        if close_listener and self._stream_server is not None:
            try:
                self._stream_server.close()
            except OSError:
                pass
            self._stream_server = None

        self._close_stream_consumers()

        # Teardown order matters: terminate the process BEFORE touching its
        # pipes. Closing stdin first would flush a BufferedWriter into a
        # full pipe that a wedged ffmpeg no longer drains — close() then
        # blocks forever while holding this lock. After terminate/kill the
        # read end closes, blocked writers get EPIPE and exit on their own.
        # _ffmpeg_lock is held across the whole teardown so the video
        # callback thread cannot spawn a fresh muxer in between (it checks
        # _running, already False, under the same lock).
        with self._ffmpeg_lock:
            self._retire_muxer_locked()
            self._video_codec = None
            self._muxer_parameter_sets = None
            self._mux_epoch = None

        self._video_queue.clear()
        self._cleanup_audio_fifo(self._audio_fifo_path)
        self._audio_fifo_path = None
        with self._audio_buffer_lock:
            self._audio_buffer.clear()

        if close_listener:
            self._stream_port = 0
        self._worker_thread = None
        self._idle_watch_thread = None
        self._ffmpeg_reader_thread = None
        self._ffmpeg_stderr_thread = None
        self._video_pacer_thread = None
        self._video_keepalive_thread = None
        self._audio_writer_thread = None
        self._coordinator.notify_stream_state_changed()

    def _start_stream_server(self) -> bool:
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            server.listen(4)
            server.settimeout(1.0)
            self._stream_port = server.getsockname()[1]
            self._stream_server = server
            self._last_client_time = time.monotonic()
            self._stream_accept_thread = threading.Thread(
                target=self._accept_stream_clients,
                name=f"cloudedge_stream_accept_{self._serial_number}",
                daemon=True,
            )
            self._stream_accept_thread.start()
            _warn_if_source_is_proxied()
            return True
        except OSError as err:
            _LOGGER.error("Failed to start local stream server for %s: %s", self._serial_number, err)
            self._stream_server = None
            self._stream_port = 0
            return False

    def _accept_stream_clients(self) -> None:
        # Each connection is answered on its own short-lived thread: a local
        # process that connects and stays silent cannot hold up HA's stream
        # worker or go2rtc until its request times out.
        pending = threading.BoundedSemaphore(_STREAM_MAX_PENDING_REQUESTS)
        while self._running or self._stream_server is not None:
            server = self._stream_server
            if server is None:
                return
            try:
                client, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                # Listening socket closed/broken: end the accept loop.
                return

            if not pending.acquire(blocking=False):
                _LOGGER.debug(
                    "Too many unanswered local stream requests for %s; closed %s",
                    self._serial_number, addr,
                )
                client.close()
                continue
            # Per-client failures must never kill the accept loop: the port
            # would stay open but unserviced for every future connection.
            try:
                threading.Thread(
                    target=self._serve_stream_client,
                    args=(client, addr, pending),
                    name=f"cloudedge_stream_request_{self._serial_number}",
                    daemon=True,
                ).start()
            except RuntimeError as err:
                pending.release()
                _LOGGER.debug(
                    "Could not answer a stream request for %s: %s", self._serial_number, err
                )
                client.close()

    def _serve_stream_client(
        self,
        client: socket.socket,
        addr: tuple[str, int],
        pending: threading.BoundedSemaphore,
    ) -> None:
        """Answer one local connection and register it if it may stream."""
        try:
            try:
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                chunked = self._answer_stream_request(client)
            finally:
                pending.release()
            if chunked is None:
                _LOGGER.debug(
                    "Refused a local stream request for %s from %s",
                    self._serial_number, addr,
                )
                client.close()
                return
            if not self._register_stream_client(client, chunked=chunked):
                return
        except OSError as err:
            _LOGGER.debug(
                "Stream client handshake failed for %s: %s", self._serial_number, err
            )
            try:
                client.close()
            except OSError:
                pass
            return

        self._last_client_time = time.monotonic()
        _LOGGER.debug("Stream client connected for %s from %s", self._serial_number, addr)
        if not self._ensure_pipeline_started():
            self._stream_client_closed(client)

    def _answer_stream_request(self, client: socket.socket) -> bool | None:
        """Answer a local client's HTTP request.

        Only a GET for this bridge's secret target receives the 200 header,
        which the MPEG-TS stream then follows. Any other client, or one whose
        request is not complete within _STREAM_REQUEST_TIMEOUT, gets a 404
        before it is registered, so it can neither wake nor watch the camera.

        Return None for a refused client, otherwise whether its stream is
        sent in chunks: every client but an HTTP/1.0 one.
        """
        deadline = time.monotonic() + _STREAM_REQUEST_TIMEOUT
        request = b""
        # Read the whole header block: a byte left unread would keep the
        # socket readable after the client's EOF, and
        # _prune_disconnected_clients_locked would never drop it.
        while b"\r\n\r\n" not in request and len(request) <= _STREAM_REQUEST_MAX_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            client.settimeout(remaining)
            try:
                chunk = client.recv(4096)
            except TimeoutError:
                break
            if not chunk:
                break
            request += chunk
        method, _, rest = request.partition(b"\r\n")[0].partition(b" ")
        target, _, version = rest.partition(b" ")
        # The last wait may have left an expired timeout on the socket.
        client.settimeout(_STREAM_REQUEST_TIMEOUT)
        if (
            b"\r\n\r\n" in request
            and method == b"GET"
            and secrets.compare_digest(target, self._stream_target.encode())
        ):
            chunked = version != b"HTTP/1.0"
            client.sendall(
                _STREAM_RESPONSE_OK if chunked else _STREAM_RESPONSE_OK_UNCHUNKED
            )
            return chunked
        client.sendall(_STREAM_RESPONSE_NOT_FOUND)
        return None

    def _ensure_pipeline_started(self) -> bool:
        """Start ffmpeg and the P2P worker on the first real stream consumer."""
        with self._lock:
            if self._running:
                return True
            if self._stream_server is None:
                return False

            cooldown_remaining = self._failure_cooldown_until - time.monotonic()
            if cooldown_remaining > 0:
                with self._metrics_lock:
                    self._stream_state = "retry_cooldown"
                    self._last_error = (
                        "Live stream temporarily paused after repeated connection "
                        f"failures; retry in {cooldown_remaining:.0f}s"
                    )
                _LOGGER.debug(
                    "Rejecting automatic stream reconnect for %s during %.0fs cooldown",
                    self._serial_number,
                    cooldown_remaining,
                )
                return False

            # HA may ask for stream_source() speculatively. Wake only after its
            # stream worker (or go2rtc) has opened the local source with its
            # secret URL, which proves there is a real consumer for this camera.
            self._kick_prewake()
            self._running = True
            self._session_generation += 1
            gen = self._session_generation
            self._got_keyframe = False
            with self._metrics_lock:
                self._stream_state = "starting"
                self._video_dropped_frames = 0
                self._audio_frames = 0
                self._audio_bytes = 0
                self._last_audio_at = 0.0
                self._last_error = None
            self._worker_thread = threading.Thread(
                target=self._stream_worker,
                args=(gen,),
                name=f"cloudedge_stream_{self._serial_number}",
                daemon=True,
            )
            self._worker_thread.start()
            self._idle_watch_thread = threading.Thread(
                target=self._idle_watch_loop,
                args=(gen,),
                name=f"cloudedge_stream_idle_{self._serial_number}",
                daemon=True,
            )
            self._idle_watch_thread.start()

        self._coordinator.notify_stream_state_changed()
        return True

    def _broadcast_stream(self, data: bytes) -> None:
        with self._stream_clients_lock:
            self._broadcast_stream_locked(data)

    def _broadcast_stream_locked(self, data: bytes) -> None:
        # Admission is nonblocking; each consumer owns its socket writer and
        # latency budget. A slow consumer cannot stall FFmpeg or another one.
        for client, consumer in list(self._stream_consumers.items()):
            if not consumer.enqueue(data):
                self._stream_consumers.pop(client, None)
                if client in self._stream_clients:
                    self._stream_clients.remove(client)
        if self._stream_clients:
            self._last_client_time = time.monotonic()

    def _register_stream_client(
        self, client: socket.socket, *, chunked: bool = False
    ) -> bool:
        consumer = StreamConsumer(client, self._stream_client_closed, chunked=chunked)
        with self._stream_clients_lock:
            # Registration and the bootstrap snapshot share the output-reader
            # lock. Every subsequent chunk follows bootstrap exactly once,
            # even if its writer has not started yet.
            self._stream_clients.append(client)
            self._stream_consumers[client] = consumer
            accepted = consumer.enqueue(bytes(self._mpegts_bootstrap))
        consumer.start()
        if not accepted:
            self._stream_client_closed(client)
        return accepted

    def _stream_client_closed(self, client: socket.socket) -> None:
        with self._stream_clients_lock:
            consumer = self._stream_consumers.pop(client, None)
            if client in self._stream_clients:
                self._stream_clients.remove(client)
            if consumer is not None:
                consumer.close()

    def _close_stream_consumers(self) -> None:
        """End the current source without acquiring the muxer lock.

        Every consumer ends its response on its own writer thread (see
        StreamConsumer.finish), so HA's stream worker reads the end of the
        stream instead of a broken connection, and this never waits for a
        socket: it also runs on HA's event loop.
        """
        with self._stream_clients_lock:
            for consumer in self._stream_consumers.values():
                consumer.finish()
            for client in self._stream_clients:
                if client in self._stream_consumers:
                    continue  # its writer closes it once the response ends
                try:
                    client.close()
                except OSError:
                    pass
            self._stream_clients.clear()
            self._stream_consumers.clear()
            self._mpegts_bootstrap.clear()
            self._bootstrap_reset.clear()

    def _retire_muxer_locked(self) -> None:
        proc = self._ffmpeg_proc
        self._ffmpeg_proc = None
        writer = self._video_writer
        self._video_writer = None
        if proc is None:
            return
        # The retired MPEG-TS output is discarded, so a graceful exit buys
        # nothing. SIGTERM cannot finish while FFmpeg's NUT demuxer is blocked
        # reading an idle stdin (the pacer cannot write under this lock), so
        # terminate() would always cost the full wait plus SIGKILL.
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        self._video_queue.clear()
        with self._audio_buffer_lock:
            self._audio_buffer.clear()
        self._last_video_write = 0.0
        self._video_write_started = 0.0

    def _start_ffmpeg_muxer(
        self, video_codec: str, *, parameter_sets: bytes = b"", first_keyframe: bytes = b"",
        generation: int | None = None, attempt_generation: int | None = None,
    ) -> bool:
        with self._ffmpeg_lock:
            def active() -> bool:
                return self._running and (
                    generation is None or generation == self._session_generation
                ) and (
                    attempt_generation is None or attempt_generation == self._attempt_generation
                )
            if not active():
                # Bridge stopped between the video callback firing and this
                # call: do not spawn an ffmpeg nobody will tear down.
                return False
            format_changed = self._video_codec is not None and (
                self._video_codec != video_codec
                or self._muxer_parameter_sets != parameter_sets
            )
            if format_changed:
                # Stop publishing the old format before ending its source.
                # A consumer can reconnect as soon as it sees EOF, while
                # this thread is still waiting for HA's notification. The
                # retired reader must not refill bootstrap or send old bytes
                # to that new connection.
                self._retire_muxer_locked()
                # HA must request its fast restart before EOF; otherwise it
                # enters the stream provider's increasing reconnect backoff.
                told = self._coordinator.notify_stream_source_changed(
                    self._serial_number, self._close_stream_consumers
                )
                if not active():
                    return False
                if told:
                    self._source_restart_until = time.monotonic() + 5.0
                else:
                    # HA's loop did not answer in time. Asking again at every
                    # IDR of the new format blocked this P2P thread, bridge
                    # and muxer locks held, for the whole wait each time.
                    # Switch now: the old source ends with EOF and HA's stream
                    # reconnects through its retry backoff, which the short
                    # reconnect window would not cover.
                    self._close_stream_consumers()
            if self._ffmpeg_proc is not None:
                if self._ffmpeg_proc.poll() is None and not format_changed:
                    return True
                self._retire_muxer_locked()
            self._video_codec = video_codec
            self._muxer_parameter_sets = parameter_sets
            self._muxer_first_keyframe = first_keyframe
            if self._mux_epoch is None:
                self._mux_epoch = time.monotonic()
            return self._start_ffmpeg_muxer_locked(video_codec)

    def _start_ffmpeg_muxer_locked(self, video_codec: str) -> bool:
        audio_fifo = self._prepare_audio_fifo() if _AUDIO_ENABLED else None
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "+igndts+discardcorrupt",
            "-probesize",
            "32768",
            "-analyzeduration",
            "1",
            "-fpsprobesize",
            "0",
            # Timestamped compressed packets retain access-unit boundaries
            # and the paced clock. A raw video demuxer probes for a frame rate
            # and timestamps buffered reads together, adding delay and bursts.
            "-use_wallclock_as_timestamps",
            "0",
            "-thread_queue_size",
            "512",
            "-f",
            "nut",
            "-i",
            "pipe:0",
        ]
        if audio_fifo:
            # Second input: G.711 µ-law from the camera via a named FIFO fed
            # at a fixed cadence (silence-filled). No wallclock timestamps
            # here: the µ-law demuxer must emit clean 0-based PTS, otherwise
            # the muxed AAC carries absolute-clock PTS and PyAV cannot infer
            # the stream profile — Home Assistant's stream worker then drops
            # the audio track ("profile is None"). The muxer only starts at
            # the first video keyframe. The writer's clock starts with the
            # muxer, including the time spent waiting for this FIFO to open.
            cmd += [
                "-thread_queue_size",
                "512",
                "-f",
                "mulaw",
                "-ar",
                "8000",
                "-ac",
                "1",
                "-i",
                audio_fifo,
            ]
        cmd += [
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            # Preserve real arrival intervals while removing absolute clock
            # values and invalid decode ordering across held frames/reconnects.
            "-bsf:v",
            "setts=pts=PTS-STARTPTS:dts=PTS-STARTPTS",
        ]
        if audio_fifo:
            # HLS in browsers cannot play raw µ-law: transcode to AAC.
            cmd += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "24k"]
        else:
            cmd += ["-an"]
        cmd += [
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            "-flush_packets",
            "1",
            "-f",
            "mpegts",
            "-mpegts_flags",
            "+resend_headers+pat_pmt_at_frames",
            "-output_ts_offset",
            f"{max(0.0, time.monotonic() - (self._mux_epoch or time.monotonic())):.6f}",
            "pipe:1",
        ]

        try:
            self._ffmpeg_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._video_writer = TimestampedVideoWriter(
                self._ffmpeg_proc.stdin,
                video_codec,
                self._muxer_first_keyframe,
                keyframe_detector=lambda data: _is_video_keyframe(
                    _split_nal_units(data), video_codec
                ),
            )
            # A retired pacer may still be waiting in get(). Give every muxer
            # its own queue so it cannot steal the new process's first IDR.
            self._video_queue = VideoBuffer()
            with self._stream_clients_lock:
                self._mpegts_bootstrap.clear()
        except FileNotFoundError:
            _LOGGER.error("ffmpeg not found in PATH; CloudEdge live stream is unavailable")
            self._ffmpeg_proc = None
            self._cleanup_audio_fifo(audio_fifo)
            return False
        except Exception as err:
            _LOGGER.error("Failed to start ffmpeg for %s: %s", self._serial_number, err)
            self._retire_muxer_locked()
            self._cleanup_audio_fifo(audio_fifo)
            return False
        self._audio_fifo_path = audio_fifo

        # Bind every helper thread to THIS ffmpeg process. Re-reading
        # self._ffmpeg_proc in the loops made old threads latch onto a
        # restarted process: two pacers interleaving writes into one stdin
        # (corrupted bitstream) and thread count growing on every crash.
        proc = self._ffmpeg_proc
        self._ffmpeg_reader_thread = threading.Thread(
            target=self._ffmpeg_stdout_reader,
            args=(proc,),
            name=f"cloudedge_ffmpeg_out_{self._serial_number}",
            daemon=True,
        )
        self._ffmpeg_reader_thread.start()
        self._ffmpeg_stderr_thread = threading.Thread(
            target=self._ffmpeg_stderr_reader,
            args=(proc,),
            name=f"cloudedge_ffmpeg_err_{self._serial_number}",
            daemon=True,
        )
        self._ffmpeg_stderr_thread.start()
        self._video_pacer_thread = threading.Thread(
            target=self._video_pacer,
            args=(proc, self._video_writer, self._video_queue),
            name=f"cloudedge_video_pacer_{self._serial_number}",
            daemon=True,
        )
        self._video_pacer_thread.start()
        self._video_keepalive_thread = threading.Thread(
            target=self._video_keepalive,
            args=(proc,),
            name=f"cloudedge_video_keepalive_{self._serial_number}",
            daemon=True,
        )
        self._video_keepalive_thread.start()
        if audio_fifo:
            self._audio_writer_thread = threading.Thread(
                target=self._audio_writer,
                args=(proc, audio_fifo),
                name=f"cloudedge_audio_writer_{self._serial_number}",
                daemon=True,
            )
            self._audio_writer_thread.start()
        return True

    def _prepare_audio_fifo(self) -> str | None:
        """Create the named FIFO used as ffmpeg's audio input.

        Each muxer gets its own private (0700) directory. A fixed name in /tmp
        collided with the FIFO left behind when Home Assistant was stopped
        mid-stream ('File exists': the next muxer ran without audio), and was
        open to other local users.
        """
        directory = None
        try:
            directory = tempfile.mkdtemp(prefix="cloudedge-audio-")
            path = os.path.join(directory, "audio.fifo")
            os.mkfifo(path, 0o600)
        except OSError as err:
            if directory is not None:
                try:
                    os.rmdir(directory)
                except OSError:
                    pass
            _LOGGER.warning(
                "Audio disabled for %s: cannot create fifo: %s",
                self._serial_number,
                err,
            )
            return None
        return path

    @staticmethod
    def _cleanup_audio_fifo(path: str | None) -> None:
        """Remove a FIFO and its private directory; safe to repeat."""
        if not path:
            return
        for remove, target in ((os.unlink, path), (os.rmdir, os.path.dirname(path))):
            try:
                remove(target)
            except OSError:
                pass

    def _audio_writer(self, proc: subprocess.Popen, fifo_path: str) -> None:
        """Feed ffmpeg's audio FIFO at a fixed 8 kB/s cadence.

        Writes real camera audio when buffered, µ-law silence otherwise, so
        the audio clock keeps running during live-window gaps and the muxer
        never stalls waiting for the audio input.
        """
        # Video can already be arriving while ffmpeg probes its first input.
        # Start the sample clock now, before the FIFO is opened, so delayed
        # opening does not permanently put audio (and the TS interleaver)
        # behind video. Catch-up uses the same bounded 20 ms chunks and keeps
        # any real samples accumulated in the existing audio buffer.
        next_write = time.monotonic()
        fd = None
        try:
            # ffmpeg opens the FIFO's read side while parsing its inputs;
            # O_NONBLOCK + retry avoids blocking forever if it dies first.
            while fd is None:
                if proc.poll() is not None:
                    return
                try:
                    fd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
                except OSError as err:
                    if err.errno != errno.ENXIO:
                        _LOGGER.debug(
                            "Audio fifo open failed for %s: %s",
                            self._serial_number,
                            err,
                        )
                        return
                    time.sleep(0.05)
            os.set_blocking(fd, True)

            silence = bytes([_MULAW_SILENCE]) * _AUDIO_CHUNK_BYTES
            while proc.poll() is None:
                with self._audio_buffer_lock:
                    if self._audio_buffer:
                        chunk = bytes(self._audio_buffer[:_AUDIO_CHUNK_BYTES])
                        del self._audio_buffer[:_AUDIO_CHUNK_BYTES]
                    else:
                        chunk = silence
                if len(chunk) < _AUDIO_CHUNK_BYTES:
                    chunk += silence[: _AUDIO_CHUNK_BYTES - len(chunk)]
                try:
                    os.write(fd, chunk)
                except (BrokenPipeError, OSError):
                    return
                next_write += _AUDIO_CHUNK_SECONDS
                delay = next_write - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._cleanup_audio_fifo(fifo_path)

    def _ffmpeg_stdout_reader(self, proc: subprocess.Popen) -> None:
        try:
            while True:
                if proc.poll() is not None or proc.stdout is None:
                    return
                data = proc.stdout.read1(32768)
                if not data:
                    return
                with self._stream_clients_lock:
                    if self._ffmpeg_proc is not proc:
                        return
                    if self._bootstrap_reset.is_set():
                        self._mpegts_bootstrap.clear()
                        self._bootstrap_reset.clear()
                    if (
                        len(self._mpegts_bootstrap) + len(data)
                        <= _MAX_MPEGTS_BOOTSTRAP_BYTES
                    ):
                        self._mpegts_bootstrap.extend(data)
                    self._broadcast_stream_locked(data)
        except Exception as err:
            _LOGGER.debug("ffmpeg stdout reader stopped for %s: %s", self._serial_number, err)

    def _ffmpeg_stderr_reader(self, proc: subprocess.Popen) -> None:
        if proc.stderr is None:
            return
        try:
            for raw_line in proc.stderr:
                line = raw_line.decode(errors="replace").rstrip()
                if line:
                    _LOGGER.debug("ffmpeg[%s]: %s", self._serial_number, line)
        except Exception:
            return

    def _video_pacer(
        self, proc: subprocess.Popen, writer: TimestampedVideoWriter,
        video_queue: VideoBuffer,
    ) -> None:
        # KCP can deliver several seconds of video in a short burst after a gap.
        # Writing that burst immediately makes ffmpeg assign compressed wall-clock
        # timestamps, so HA freezes and then fast-forwards. Space writes using the
        # measured source cadence; drain a backlog proportionally faster, never
        # in one burst.
        #
        # During a video gap FFmpeg emits nothing, audio included: HA's stream
        # worker fails after 30 s without data and its reconnect then reports a
        # timestamp discontinuity. Re-feed the last real IDR written here,
        # never a predicted frame: it is a complete decoder entry point and its
        # wall-clock timestamp keeps the output continuous. Admission closes
        # first, so frames predicted from the pictures before the gap wait for
        # the camera's next IDR. Re-fed frames bypass on_video and VideoBuffer:
        # frame, gap and drop counts and _last_real_video see only the camera.
        next_write_at = 0.0
        held_keyframe: bytes | None = None
        refeed_at = 0.0
        while True:
            if proc.poll() is not None or proc.stdin is None:
                return
            timeout = 0.5
            if held_keyframe is not None:
                delay = self._keyframe_refeed_delay(refeed_at)
                if delay is not None:
                    timeout = min(timeout, delay)
            try:
                data, keyframe = video_queue.get(timeout=timeout)
            except queue.Empty:
                if not self._running:
                    return
                if (
                    held_keyframe is None
                    or self._keyframe_refeed_delay(refeed_at) != 0.0
                ):
                    continue
                try:
                    with self._ffmpeg_lock:
                        if self._ffmpeg_proc is not proc or not self._running:
                            return
                        if not video_queue.require_keyframe_if_idle():
                            continue  # the camera is back
                        self._bootstrap_reset.set()
                        # The stall watchdog covers this write too: FFmpeg
                        # can wedge during a gap as well.
                        self._video_write_started = time.monotonic()
                    writer.write(held_keyframe)
                    writer.flush()
                    with self._ffmpeg_lock:
                        if self._ffmpeg_proc is not proc:
                            return
                        self._video_write_started = 0.0
                except (BrokenPipeError, OSError, ValueError):
                    return
                refeed_at = time.monotonic() + _KEEPALIVE_INTERVAL
                continue

            try:
                now = time.monotonic()
                if next_write_at > now:
                    time.sleep(next_write_at - now)
                with self._ffmpeg_lock:
                    if self._ffmpeg_proc is not proc or not self._running:
                        return
                    if keyframe:
                        self._bootstrap_reset.set()
                    self._video_write_started = time.monotonic()
                writer.write(data)
                writer.flush()
                with self._ffmpeg_lock:
                    if self._ffmpeg_proc is not proc:
                        return
                    self._last_video_write = written_at = time.monotonic()
                    self._video_write_started = 0.0
                if keyframe:
                    held_keyframe = data
                with self._metrics_lock:
                    pacer_fps = self._pacer_fps
                # Advance from the previous deadline, so write time and sleep
                # overshoot never accumulate into latency, and drain a backlog
                # proportionally faster (each queued second adds 1x) before
                # VideoBuffer's age limit retires the whole GOP.
                backlog_seconds = video_queue.qsize() / pacer_fps
                interval = 1.0 / (pacer_fps * (1.0 + backlog_seconds))
                next_write_at = max(next_write_at + interval, written_at)
            except (BrokenPipeError, OSError, ValueError):
                return

    def _keyframe_refeed_delay(self, refeed_at: float) -> float | None:
        """Seconds until the held IDR is due in a video gap; None if not held.

        Nothing is held once the bridge stops, while repeated failures pause
        the camera, or after _KEEPALIVE_MAX_HOLD without real video.
        """
        now = time.monotonic()
        last_video = self._last_real_video
        if (
            not self._running
            or self._failure_cooldown_until > now
            or now - last_video > _KEEPALIVE_MAX_HOLD
        ):
            return None
        return max(0.0, last_video + _KEEPALIVE_GAP - now, refeed_at - now)

    def _video_keepalive(self, proc: subprocess.Popen) -> None:
        # Watch this muxer's process. Video gaps are bridged by the pacer, the
        # only writer, which re-feeds the held IDR (see _video_pacer).
        started = time.monotonic()
        while True:
            if proc.poll() is not None:
                # FFmpeg exited by itself (crash, OOM kill): its pacer and
                # reader are gone, yet the keyframe gate is still open, so no
                # frame would ever start a replacement. Stop and format
                # changes retire the process first and do not get here.
                with self._ffmpeg_lock:
                    if self._ffmpeg_proc is proc and self._running:
                        if time.monotonic() - started < _MUXER_QUICK_EXIT:
                            self._muxer_quick_exits += 1
                        else:
                            self._muxer_quick_exits = 0
                        if self._muxer_quick_exits < _MUXER_QUICK_EXITS:
                            _LOGGER.warning(
                                "ffmpeg for %s exited unexpectedly (code %s) — "
                                "restarting the muxer at the next keyframe",
                                self._serial_number,
                                proc.returncode,
                            )
                        else:
                            _LOGGER.error(
                                "ffmpeg for %s exited right after each of its "
                                "last %d starts (code %s); no live video until "
                                "the camera's next session",
                                self._serial_number,
                                self._muxer_quick_exits,
                                proc.returncode,
                            )
                        self._restart_muxer_at_next_keyframe_locked()
                return
            time.sleep(0.5)
            if not self._running or self._ffmpeg_proc is not proc:
                return

            # Muxer watchdog: bad input timestamps can wedge ffmpeg (mux
            # errors, stops reading stdin) while the process stays alive —
            # the pacer blocks on a full pipe and every new frame gets
            # dropped as "backlogged" forever. Frames queued but nothing
            # written for too long means exactly that: kill this ffmpeg so
            # the pipeline restarts on the next keyframe.
            with self._ffmpeg_lock:
                if self._ffmpeg_proc is not proc or not self._running:
                    return
                stall_now = time.monotonic()
                if (
                    self._video_write_started
                    and stall_now - self._video_write_started > _MUX_STALL_TIMEOUT
                ):
                    _LOGGER.warning(
                        "ffmpeg for %s stopped consuming input for %.0fs — "
                        "restarting the muxer",
                        self._serial_number,
                        stall_now - self._video_write_started,
                    )
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    self._restart_muxer_at_next_keyframe_locked()
                    return

    def _restart_muxer_at_next_keyframe_locked(self) -> None:
        """Drop input queued for a dead or wedged muxer and reopen the gate.

        on_video starts a muxer only while _got_keyframe is False: the
        camera's next IDR then starts a fresh FFmpeg on the same HA socket,
        unless this attempt's FFmpeg processes kept exiting at once.
        """
        self._video_queue.clear()
        self._got_keyframe = False
        with self._audio_buffer_lock:
            self._audio_buffer.clear()
        # A stale timestamp must not trip the next watchdog before the new
        # pacer's first write.
        self._last_video_write = 0.0
        self._video_write_started = 0.0

    def _enqueue_video(self, data: bytes, *, reset_bootstrap: bool = False) -> bool:
        accepted = self._video_queue.put(data, reset_bootstrap)
        if not accepted:
            # stream_dropped_frames counts these admission refusals only.
            # Frames discarded after admission (with a retired GOP, or a
            # cleared queue) are not counted. Frame rate and gaps are measured
            # at receipt, so these refusals are what show FFmpeg or the pacer
            # behind, or a gap bridged with the held IDR (predicted frames
            # until the camera's next IDR).
            with self._metrics_lock:
                self._video_dropped_frames += 1
            _LOGGER.debug(
                "Dropping video frame for %s: ffmpeg is backlogged or a keyframe "
                "is awaited",
                self._serial_number,
            )
        return accepted

    def _get_stream_device(self) -> dict | None:
        device = self._coordinator.get_device_for_stream(self._serial_number)
        if device is None:
            _LOGGER.warning("No device data available for live stream %s", self._serial_number)
        return device

    def _record_stream_attempt(
        self,
        *,
        video_id: int,
        frames: int,
        total_bytes: int,
        duration: float,
        media_duration: float,
        max_frame_gap: float,
        logged_in: bool,
    ) -> None:
        fps = ((frames - 1) / media_duration) if frames > 1 and media_duration > 0 else 0.0
        with self._metrics_lock:
            self._last_session_frames = frames
            self._last_session_bytes = total_bytes
            self._last_session_duration = duration
            self._last_session_fps = fps
            self._last_max_frame_gap = max_frame_gap
            if fps > 0:
                self._pacer_fps = max(
                    _PACER_MIN_FPS,
                    min(_PACER_MAX_FPS, fps),
                )

        if self._stream_profile != STREAM_PROFILE_AUTO:
            return

        if (
            video_id == self._substream_video_id
            and self._substream_video_id != self._main_video_id
        ):
            if frames:
                self._auto_substream_failures = 0
                return
            if logged_in:
                self._auto_substream_failures += 1
            if self._auto_substream_failures >= _AUTO_SUBSTREAM_FAILURES:
                _LOGGER.info(
                    "Substream produced no video for %s; returning Auto to main stream",
                    self._serial_number,
                )
                self._active_video_id = self._main_video_id
                self._auto_degraded_windows = 0
            return

        degraded = (
            logged_in
            and frames >= 5
            and (
                fps < _AUTO_MAX_SUSTAINABLE_FPS
                or max_frame_gap > _AUTO_MAX_FRAME_GAP
            )
        )
        if not degraded:
            self._auto_degraded_windows = 0
            return

        self._auto_degraded_windows += 1
        if (
            self._substream_video_id != self._main_video_id
            and self._auto_degraded_windows >= _AUTO_FALLBACK_WINDOWS
        ):
            _LOGGER.info(
                "Auto profile switching %s to substream after %d degraded windows "
                "(%.2f fps, %.2fs max gap)",
                self._serial_number,
                self._auto_degraded_windows,
                fps,
                max_frame_gap,
            )
            self._active_video_id = self._substream_video_id
            self._auto_substream_failures = 0

    def _resolve_active_video_id(self, device: dict) -> int:
        """Resolve Auto/HD/SD against the camera's advertised profiles."""
        hd_id, sd_id, available = resolve_stream_profile_ids(device)
        auto_id, auto_fallback_id = resolve_auto_stream_ids(device)
        self._main_video_id = auto_id
        self._substream_video_id = auto_fallback_id
        self._hd_video_id = hd_id
        self._sd_video_id = sd_id
        self._available_video_ids = available

        if self._stream_profile == STREAM_PROFILE_MAIN:
            selected = hd_id
        elif self._stream_profile == STREAM_PROFILE_SUBSTREAM:
            selected = sd_id
        elif self._active_video_id not in (auto_id, auto_fallback_id):
            selected = auto_id
        else:
            selected = self._active_video_id

        self._active_video_id = selected
        return selected

    def _resolve_signaling_server_for_api(self, api) -> tuple[str, int]:
        """Resolve the signaling host from the account region."""
        candidates: list[tuple[str, int]] = []
        openapi_base = getattr(api, "OPENAPI_BASE_URL", "")
        openapi_host = urlparse(openapi_base).hostname or ""
        if (
            openapi_host.startswith("openapi-")
            and openapi_host.endswith(".mearicloud.com")
        ):
            signaling_host = openapi_host[len("openapi-"):]
            candidates.append((signaling_host, 28974))
            candidates.append((signaling_host, 9253))

        candidates.extend(
            [
                ("euce.mearicloud.com", 28974),
                ("47.254.142.96", 28974),
            ]
        )

        seen: set[tuple[str, int]] = set()
        for host, port in candidates:
            if (host, port) in seen:
                continue
            seen.add((host, port))
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                sock.connect((host, port))
                sock.close()
                return (host, port)
            except (socket.timeout, ConnectionRefusedError, OSError):
                continue

        return candidates[0]

    def _patch_streamer_status_retry(self, streamer) -> None:
        """Retry P2P signaling status when OpenAPI already says online.

        Some cameras briefly report ``offline`` on the signaling channel even
        after ``get_device_online_status()`` has switched to ``online``. In
        that window the stock streamer aborts too early.
        """
        if getattr(streamer, "_cloudedge_status_retry_patch", False):
            return

        original_do_stream = streamer._do_stream
        original_on_disconnect = streamer.on_disconnect

        def _do_stream_with_status_retry(sig):
            original_query_status = sig.query_device_status

            def _query_status_with_retry(device_uuid):
                status = original_query_status(device_uuid)
                if status.get("status") != "offline":
                    return status

                api = getattr(streamer, "_api", None)
                serial_number = getattr(streamer, "_sn_num", None) or self._serial_number
                if api is None or not hasattr(api, "get_device_online_status"):
                    return status

                try:
                    api_status = api.get_device_online_status(serial_number)
                except Exception as err:
                    _LOGGER.debug(
                        "OpenAPI status recheck failed for %s during P2P startup: %s",
                        serial_number,
                        err,
                    )
                    return status

                if api_status != "online":
                    return status

                _LOGGER.debug(
                    "Signaling status is offline for %s while OpenAPI is online; "
                    "retrying signaling status for %.0fs",
                    serial_number,
                    _SIGNAL_STATUS_RETRY_WINDOW,
                )
                deadline = time.monotonic() + _SIGNAL_STATUS_RETRY_WINDOW
                while time.monotonic() < deadline and getattr(streamer, "_running", False):
                    time.sleep(_SIGNAL_STATUS_RETRY_POLL)
                    retry_status = original_query_status(device_uuid)
                    retry_name = retry_status.get("status", "unknown")
                    if retry_name != "offline":
                        _LOGGER.debug(
                            "Signaling status recovered for %s: %s",
                            serial_number,
                            retry_name,
                        )
                        return retry_status

                return status

            sig.query_device_status = _query_status_with_retry
            return original_do_stream(sig)

        def _run_session_with_region_signaling():
            from cloudedge.p2p.meari_signaling import MsgSvrClient

            streamer._running = True
            streamer._video_count = 0
            streamer._total_bytes = 0

            sig = None
            try:
                sig_host, sig_port = self._resolve_signaling_server_for_api(streamer._api)
                _LOGGER.debug(
                    "Connecting to signaling %s:%d for %s",
                    sig_host,
                    sig_port,
                    self._serial_number,
                )
                sig = MsgSvrClient(sig_host, sig_port)
                sig.connect()

                video_count, total_bytes = streamer._do_stream(sig)
                streamer._video_count = video_count
                streamer._total_bytes = total_bytes
                return (video_count, total_bytes)
            except Exception as err:
                _LOGGER.error("P2P session error: %s", err)
                return (streamer._video_count, streamer._total_bytes)
            finally:
                if sig:
                    try:
                        sig.send_logout(streamer._device_uuid)
                    except Exception:
                        pass
                    sig.close()
                if original_on_disconnect:
                    try:
                        original_on_disconnect()
                    except Exception:
                        pass

        streamer._do_stream = _do_stream_with_status_retry
        streamer.run_session = _run_session_with_region_signaling
        streamer._cloudedge_status_retry_patch = True

    def _stream_worker(self, gen: int) -> None:
        consecutive_failures = 0
        device: dict | None = None
        attempt_number = 0
        stop_reason = "worker_exit"

        def _session_active() -> bool:
            return self._running and gen == self._session_generation

        try:
            while _session_active() and self._should_keep_running():
                client = self._coordinator.client
                device = self._get_stream_device()
                if client is not None and device is not None:
                    break
                time.sleep(2)
            else:
                return

            if not hasattr(client, "create_streamer"):
                _LOGGER.error(
                    "Installed pycloudedge build does not support P2P streaming; "
                    "install the local repository build inside Home Assistant",
                )
                return

            with self._metrics_lock:
                self._stream_state = "waking"
            self._coordinator.notify_stream_state_changed()
            prewake_result = self._wait_for_prewake()
            wake_completed = (
                self._wake_camera_if_needed(device)
                if prewake_result is None
                else prewake_result
            )
            if not wake_completed:
                _LOGGER.debug(
                    "Wake preparation did not confirm %s online; "
                    "continuing with P2P signaling",
                    self._serial_number,
                )

            if not _session_active():
                return

            try:
                create_parameters = inspect.signature(client.create_streamer).parameters
            except (TypeError, ValueError):
                create_parameters = {}
            # IoT 167 is FRONT_LIGHT_SWITCH (the floodlight), not a live-view
            # flag: the vendor app never writes it to start live view and the
            # cameras stream the same window without it. pycloudedge <= 0.1.11
            # toggles it by default, so opt out explicitly.
            supports_external_switch = "manage_stream_switch" in create_parameters

            while _session_active():
                if not self._should_keep_running():
                    return

                client = self._coordinator.client
                device = self._get_stream_device()
                if client is None or device is None:
                    time.sleep(2)
                    continue

                with self._lock:
                    if not _session_active():
                        return
                    attempt_logged_in = False
                    attempt_codec: str | None = None
                    parameter_cache: dict[int, bytes] = {}
                    self._attempt_generation += 1
                    token = self._attempt_generation
                    attempt_frames = 0
                    attempt_bytes = 0
                    attempt_started = time.monotonic()
                    first_frame_at = 0.0
                    last_frame_at = 0.0
                    max_frame_gap = 0.0
                    attempt_number += 1
                    if attempt_number > 1:
                        with self._metrics_lock:
                            self._reconnect_count += 1
                    self._got_keyframe = False
                    self._muxer_quick_exits = 0
                    self._profile_changed.clear()
                    video_id = self._resolve_active_video_id(device)
                    with self._metrics_lock:
                        self._stream_state = "connecting"
                        if (
                            video_id == self._substream_video_id
                            and self._substream_video_id != self._main_video_id
                        ):
                            self._pacer_fps = _SUBSTREAM_INITIAL_FPS
                self._coordinator.notify_stream_state_changed()

                def _attempt_active(callback_token: int) -> bool:
                    return _session_active() and callback_token == self._attempt_generation

                def on_video(frame_data: bytes, _token: int = token) -> None:
                    nonlocal attempt_frames, attempt_bytes, attempt_codec
                    nonlocal first_frame_at, last_frame_at, max_frame_gap
                    if not _attempt_active(_token):
                        return
                    frame_data = _convert_to_annex_b(frame_data)
                    # Split once: every decision below reads these units.
                    nal_units = _split_nal_units(frame_data)
                    detected_codec = _detect_video_codec(nal_units)
                    if detected_codec is not None and detected_codec != attempt_codec:
                        # Detection belongs to this P2P attempt, never to the
                        # retired muxer. Profiles can change codec/resolution.
                        attempt_codec = detected_codec
                        parameter_cache.clear()
                        self._got_keyframe = False
                    codec = attempt_codec
                    if codec is None:
                        return
                    required = (7, 8) if codec == "h264" else (32, 33, 34)
                    slices = []
                    parameters_changed = False
                    for nal in nal_units:
                        nal_type = (nal[0] & 0x1F) if codec == "h264" else ((nal[0] >> 1) & 0x3F)
                        if nal_type in required:
                            encoded = b"\x00\x00\x00\x01" + nal
                            if parameter_cache.get(nal_type) != encoded:
                                parameters_changed = True
                            parameter_cache[nal_type] = encoded
                        else:
                            slices.append(b"\x00\x00\x00\x01" + nal)
                    if parameters_changed and self._got_keyframe:
                        self._got_keyframe = False
                    is_keyframe = _is_video_keyframe(nal_units, codec)
                    has_slice = any(
                        (1 <= nal[0] & 0x1F <= 5) if codec == "h264"
                        else ((nal[0] >> 1) & 0x3F) <= 31
                        for nal in nal_units
                    )
                    if not has_slice:
                        return
                    if not self._got_keyframe and not is_keyframe:
                        return
                    if any(kind not in parameter_cache for kind in required):
                        return
                    parameter_sets = b"".join(parameter_cache[kind] for kind in required)
                    if is_keyframe:
                        # Every decoder entry point is self-contained, including
                        # separately delivered parameter sets and reconnects.
                        frame_data = parameter_sets + b"".join(slices)
                    if not self._got_keyframe:
                        if self._muxer_quick_exits >= _MUXER_QUICK_EXITS:
                            return  # FFmpeg cannot run: see _video_keepalive
                        if not self._start_ffmpeg_muxer(
                            codec, parameter_sets=parameter_sets, first_keyframe=frame_data,
                            generation=gen, attempt_generation=_token,
                        ):
                            _LOGGER.error("Failed to start %s muxer for %s", codec, self._serial_number)
                            return
                        self._got_keyframe = True
                    # Measure the network source at receipt. Frames VideoBuffer
                    # rejects after its own overload retirement must not read
                    # as network gaps or a lower source rate (Auto profile and
                    # pacer cadence would otherwise feed back on the drops).
                    received_at = time.monotonic()
                    attempt_frames += 1
                    attempt_bytes += len(frame_data)
                    if not first_frame_at:
                        first_frame_at = received_at
                        self._failure_cooldown_until = 0.0
                        self._consecutive_stream_failures = 0
                        with self._metrics_lock:
                            self._stream_state = "streaming"
                        self._coordinator.notify_stream_state_changed()
                        _LOGGER.info("First live %s keyframe received for %s (%d bytes)", codec, self._serial_number, len(frame_data))
                    if last_frame_at:
                        max_frame_gap = max(max_frame_gap, received_at - last_frame_at)
                    last_frame_at = received_at
                    self._last_real_video = received_at
                    media_span = received_at - first_frame_at
                    if attempt_frames >= 5 and media_span >= 2.0:
                        observed_fps = max(_PACER_MIN_FPS, min(_PACER_MAX_FPS, (attempt_frames - 1) / media_span))
                        with self._metrics_lock:
                            self._pacer_fps = self._pacer_fps * 0.5 + observed_fps * 0.5
                    self._enqueue_video(frame_data, reset_bootstrap=is_keyframe)

                def on_audio(audio_data: bytes, _token: int = token) -> None:
                    if not _attempt_active(_token) or not self._got_keyframe or not audio_data:
                        return
                    with self._metrics_lock:
                        self._audio_frames += 1
                        self._audio_bytes += len(audio_data)
                        self._last_audio_at = time.monotonic()
                    with self._audio_buffer_lock:
                        self._audio_buffer.extend(audio_data)
                        excess = len(self._audio_buffer) - _AUDIO_BUFFER_MAX_BYTES
                        if excess > 0:
                            del self._audio_buffer[:excess]

                def on_login(_token: int = token) -> None:
                    nonlocal attempt_logged_in
                    if not _attempt_active(_token):
                        return
                    attempt_logged_in = True
                    self._last_online_confirmation = time.monotonic()
                    self._coordinator.set_runtime_connection_status(self._serial_number, "online")
                    # Authentication alone does not prove the media path works.
                    # Retry accounting and streaming state change on valid video.
                    _LOGGER.info("Live stream authenticated for %s", self._serial_number)
                    self._coordinator.notify_stream_state_changed()

                def on_disconnect(_token: int = token) -> None:
                    if not _attempt_active(_token):
                        return
                    with self._metrics_lock:
                        self._stream_state = "reconnecting"
                    _LOGGER.info("Live stream disconnected for %s", self._serial_number)
                    self._coordinator.notify_stream_state_changed()

                def guarded(callback, callback_token: int):
                    def invoke(*args):
                        # Stop/start and media admission share one order:
                        # bridge lock -> muxer lock -> consumer lock. A callback
                        # paused before startup cannot resurrect retired media.
                        with self._lock:
                            if _attempt_active(callback_token):
                                callback(*args)
                    return invoke

                on_video = guarded(on_video, token)
                on_audio = guarded(on_audio, token)
                on_login = guarded(on_login, token)
                on_disconnect = guarded(on_disconnect, token)

                streamer = None
                try:
                    create_kwargs = dict(
                        device=device,
                        on_video=on_video,
                        on_audio=on_audio,
                        on_login=on_login,
                        on_disconnect=on_disconnect,
                        video_id=video_id,
                    )
                    if _AUDIO_ENABLED and "on_audio" in create_parameters:
                        create_kwargs["on_audio"] = on_audio
                    if supports_external_switch:
                        create_kwargs["manage_stream_switch"] = False
                    streamer = client.create_streamer(**create_kwargs)
                    if streamer is None:
                        _LOGGER.error("Failed to create P2P streamer for %s", self._serial_number)
                        return
                    with self._lock:
                        if not _attempt_active(token):
                            streamer.request_stop()
                            if _session_active():
                                # Only this attempt was retired (profile
                                # change): start the next one, keep the session.
                                continue
                            return
                        self._p2p_streamer = streamer
                    _LOGGER.debug(
                        "Using CloudEdge video_id=%d for %s",
                        video_id,
                        self._serial_number,
                    )
                    if not getattr(
                        streamer,
                        "_cloudedge_native_signaling_retry",
                        False,
                    ):
                        self._patch_streamer_status_retry(streamer)
                    streamer.run_session()
                except Exception as err:
                    with self._metrics_lock:
                        self._last_error = str(err)
                    _LOGGER.error("P2P stream failed for %s: %s", self._serial_number, err)
                finally:
                    with self._lock:
                        if gen == self._session_generation:
                            if token == self._attempt_generation:
                                self._attempt_generation += 1
                            if self._p2p_streamer is streamer:
                                self._p2p_streamer = None

                if gen != self._session_generation:
                    return
                attempt_duration = time.monotonic() - attempt_started
                media_duration = (
                    last_frame_at - first_frame_at
                    if first_frame_at and last_frame_at > first_frame_at
                    else 0.0
                )
                self._record_stream_attempt(
                    video_id=video_id,
                    frames=attempt_frames,
                    total_bytes=attempt_bytes,
                    duration=attempt_duration,
                    media_duration=media_duration,
                    max_frame_gap=max_frame_gap,
                    logged_in=attempt_logged_in,
                )
                self._coordinator.notify_stream_state_changed()

                if attempt_frames:
                    self._consecutive_stream_failures = 0
                elif self._bridge_stopped_attempt >= token:
                    # Stopped by the bridge, not a camera failure. Attempts run
                    # one at a time and generations only grow, so a mark at or
                    # past this token was set after this attempt began: by a
                    # stop of this attempt (a second one, while it was still
                    # closing, records a later generation) or by a profile
                    # change just after it ended, which resets the count.
                    pass
                else:
                    self._consecutive_stream_failures += 1
                    if (
                        self._consecutive_stream_failures
                        >= _MAX_CONSECUTIVE_STREAM_FAILURES
                    ):
                        cooldown = _failed_session_cooldown(
                            self._consecutive_stream_failures
                        )
                        self._failure_cooldown_until = time.monotonic() + cooldown
                        with self._metrics_lock:
                            self._stream_state = "retry_cooldown"
                            self._last_error = (
                                "Live stream failed repeatedly; automatic retries "
                                f"paused for {cooldown:.0f}s"
                            )
                        _LOGGER.warning(
                            "Live stream for %s failed %d times in a row; pausing "
                            "automatic retries for %.0fs",
                            self._serial_number,
                            self._consecutive_stream_failures,
                            cooldown,
                        )
                        # The stop reason is the state for the whole pause.
                        stop_reason = "retry_cooldown"
                        return

                # Keep the local listener available for the idle grace period,
                # but do not consume battery by opening another P2P window when
                # Home Assistant no longer has an active MPEG-TS consumer.
                if not self._wait_for_stream_consumer(gen):
                    return
                if not self._should_keep_running():
                    return

                # A finished window or a cooldown-collided attempt both land here.
                # The camera rate-limits back-to-back sessions, so a single failed
                # attempt is expected — wait the cooldown and re-establish a fresh
                # window rather than giving up. Bail only after repeated failures
                # (camera genuinely offline / asleep).
                if self._profile_changed.is_set():
                    continue

                # Responsive cooldown: bail early if all consumers disappear.
                cooldown_until = time.monotonic() + _RECONNECT_COOLDOWN
                while time.monotonic() < cooldown_until:
                    if (
                        not _session_active()
                        or not self._wait_for_stream_consumer(gen)
                        or not self._should_keep_running()
                    ):
                        return
                    if self._profile_changed.is_set():
                        break
                    time.sleep(0.5)
        finally:
            # Only tear down the pipeline we belong to: a stale worker from
            # a previous session must not stop the live one.
            with self._lock:
                if gen == self._session_generation:
                    self._stop_locked(stop_reason)

    def _wake_camera_if_needed(self, device: dict) -> bool:
        client = self._coordinator.client
        if client is None or not hasattr(client, "wake_device"):
            return True

        serial_number = device.get("serial_number") or self._serial_number
        device_id = device.get("device_id")
        try:
            now = time.monotonic()
            if (now - self._last_online_confirmation) < _ONLINE_GRACE_WINDOW:
                return True

            if hasattr(client, "get_device_online_status"):
                try:
                    if client.get_device_online_status(serial_number) == "online":
                        self._last_online_confirmation = now
                        self._coordinator.set_runtime_connection_status(serial_number, "online")
                        return True
                except Exception:
                    pass

            if (now - self._last_wake_attempt) < _WAKE_RETRY_COOLDOWN:
                _LOGGER.debug(
                    "Skipping wake for %s; last wake was %.0fs ago",
                    serial_number,
                    now - self._last_wake_attempt,
                )
                return True

            self._last_wake_attempt = now
            with self._metrics_lock:
                self._wake_count += 1
            _LOGGER.debug("Sending wake signal before live stream for %s", serial_number)
            client.wake_device(serial_number, device_id)
            if hasattr(client, "wait_for_online"):
                is_online = bool(
                    client.wait_for_online(
                        serial_number,
                        timeout=20.0,
                        poll_interval=2.0,
                    )
                )
                if is_online:
                    self._last_online_confirmation = time.monotonic()
                    self._coordinator.set_runtime_connection_status(serial_number, "online")
                return is_online
            time.sleep(5)
            self._last_online_confirmation = time.monotonic()
            self._coordinator.set_runtime_connection_status(serial_number, "online")
            return True
        except Exception as err:
            _LOGGER.debug("Wake before live stream failed for %s: %s", serial_number, err)
            return False

    def _should_keep_running(self) -> bool:
        if not self._running:
            return False
        now = time.monotonic()
        if self.client_count:
            return True
        return (now - max(self._last_request_time, self._last_client_time)) < _IDLE_TIMEOUT

    def _wait_for_stream_consumer(self, gen: int) -> bool:
        """Allow only a format restart a short reconnect window."""
        while self._running and gen == self._session_generation:
            if self.client_count:
                return True
            if time.monotonic() >= self._source_restart_until:
                return False
            time.sleep(0.05)
        return False

    def _idle_watch_loop(self, gen: int) -> None:
        while self._running and gen == self._session_generation:
            time.sleep(1)
            if self._should_keep_running():
                continue
            _LOGGER.debug("Stopping idle live stream for %s", self._serial_number)
            with self._lock:
                streamer = self._p2p_streamer
                if streamer is not None:
                    self._bridge_stopped_attempt = self._attempt_generation
            if streamer is not None:
                try:
                    streamer.request_stop()
                except Exception:
                    pass
            else:
                with self._lock:
                    if gen == self._session_generation:
                        self._stop_locked("idle_timeout")
                return


class CloudEdgeStreamManager:
    """Manage per-device stream bridges for a CloudEdge account."""

    def __init__(self, coordinator: CloudEdgeCoordinator) -> None:
        self._coordinator = coordinator
        self._bridges: dict[str, CloudEdgeStreamBridge] = {}
        self._lock = threading.Lock()

    def get_bridge(self, serial_number: str) -> CloudEdgeStreamBridge:
        with self._lock:
            bridge = self._bridges.get(serial_number)
            if bridge is None:
                bridge = CloudEdgeStreamBridge(self._coordinator, serial_number)
                self._bridges[serial_number] = bridge
            return bridge

    def is_streaming(self, serial_number: str) -> bool:
        """Return True if an existing bridge for the device is active."""
        with self._lock:
            bridge = self._bridges.get(serial_number)
        return bool(bridge and bridge.is_streaming)

    def get_profile(self, serial_number: str) -> str:
        """Return the requested profile, creating the bridge if needed."""
        return self.get_bridge(serial_number).stream_profile

    def set_profile(self, serial_number: str, profile: str) -> None:
        """Set the requested profile for one camera."""
        self.get_bridge(serial_number).set_stream_profile(profile)

    def diagnostics(self, serial_number: str) -> dict[str, object]:
        """Return diagnostics without starting a new bridge."""
        with self._lock:
            bridge = self._bridges.get(serial_number)
        # As an idle bridge: no stream opened, the camera's profiles unknown.
        return bridge.diagnostics() if bridge else {
            "stream_profile": STREAM_PROFILE_AUTO,
            "stream_profile_active": None,
            "stream_video_id": None,
            "stream_main_video_id": None,
            "stream_substream_video_id": None,
            "stream_available_video_ids": [],
            "stream_state": "idle",
            "stream_dropped_frames": 0,
            "stream_audio_codec": "pcm_mulaw/8000",
            "stream_audio_frames": 0,
            "stream_audio_bytes": 0,
            "stream_audio_active": False,
        }

    def stop_all(self) -> None:
        """Stop every bridge, then let their P2P sessions finish closing.

        Runs on unload and on Home Assistant stop, from the executor. The join
        is bounded (one deadline for all bridges): a worker stuck in network
        I/O must not hold up the unload or the shutdown.
        """
        with self._lock:
            bridges = list(self._bridges.values())
        workers = [bridge.stop("manager_stop") for bridge in bridges]
        deadline = time.monotonic() + _STOP_JOIN_TIMEOUT
        for worker in workers:
            if worker is not None and worker is not threading.current_thread():
                worker.join(max(0.0, deadline - time.monotonic()))

    def remove_missing(self, serial_numbers: set[str]) -> None:
        with self._lock:
            missing = [sn for sn in self._bridges if sn not in serial_numbers]
            bridges = [self._bridges.pop(sn) for sn in missing]
        for bridge in bridges:
            bridge.stop("device_removed")
