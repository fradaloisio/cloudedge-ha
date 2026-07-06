"""Per-camera live stream bridge for CloudEdge battery cameras.

This module keeps the Home Assistant camera entity thin. The entity exposes a
``stream_source()`` URL, while this bridge owns the blocking pieces:

* CloudEdge P2P streaming via ``pycloudedge``
* H.264/HEVC bitstream inspection
* ``ffmpeg`` remuxing from raw video into MPEG-TS
* a tiny local TCP server that Home Assistant can open via ``tcp://...``
"""
from __future__ import annotations

import inspect
import logging
import queue
import socket
import subprocess
import threading
import time
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from . import CloudEdgeCoordinator

_LOGGER = logging.getLogger(__name__)

_IDLE_TIMEOUT = 45.0
# The camera grants finite ~15-19s live windows and rate-limits immediate
# re-establishment (a reconnect within ~1-2s gets no video; waiting ~8s makes
# every window succeed). Pace window-to-window reconnects by this cooldown.
_RECONNECT_COOLDOWN = 8.0
_MAX_CONSECUTIVE_STREAM_FAILURES = 4
# Stop re-feeding the held keyframe after this long without any real video, so a
# camera that has genuinely gone offline surfaces as a stalled/errored stream
# instead of an indefinitely frozen "live" image.
_KEEPALIVE_MAX_HOLD = 30.0
_WAKE_RETRY_COOLDOWN = 15.0
_PREWAKE_RESULT_TTL = 30.0
_ONLINE_GRACE_WINDOW = 20.0
_SIGNAL_STATUS_RETRY_WINDOW = 8.0
_SIGNAL_STATUS_RETRY_POLL = 1.0
_MAX_MPEGTS_BOOTSTRAP_BYTES = 4 * 1024 * 1024
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


def _annex_b_nal_headers(frame_data: bytes):
    """Yield the first byte of each Annex B NAL unit."""
    i = 0
    while i < len(frame_data) - 5:
        if frame_data[i:i + 4] == b"\x00\x00\x00\x01":
            yield frame_data[i + 4]
            i += 5
            continue
        if frame_data[i:i + 3] == b"\x00\x00\x01":
            yield frame_data[i + 3]
            i += 4
            continue
        i += 1


def _detect_video_codec(frame_data: bytes) -> str | None:
    """Detect H.264 or HEVC from parameter-set NAL units."""
    headers = list(_annex_b_nal_headers(frame_data))
    h264_types = {header & 0x1F for header in headers}
    if 7 in h264_types or 8 in h264_types:
        return "h264"
    hevc_types = {(header >> 1) & 0x3F for header in headers}
    if hevc_types.intersection({32, 33, 34}):
        return "hevc"
    return None


def _is_video_keyframe(frame_data: bytes, codec: str) -> bool:
    """Return True if the access unit contains a codec keyframe."""
    headers = list(_annex_b_nal_headers(frame_data))
    if codec == "h264":
        return any((header & 0x1F) in {5, 7, 8} for header in headers)
    return any(((header >> 1) & 0x3F) in {19, 20, 32, 33, 34} for header in headers)


class CloudEdgeStreamBridge:
    """Bridge one CloudEdge camera to a local MPEG-TS TCP endpoint."""

    def __init__(self, coordinator: CloudEdgeCoordinator, serial_number: str) -> None:
        self._coordinator = coordinator
        self._serial_number = serial_number

        self._lock = threading.Lock()
        self._running = False
        self._got_keyframe = False

        self._stream_port = 0
        self._stream_server: socket.socket | None = None
        self._stream_accept_thread: threading.Thread | None = None
        self._stream_clients: list[socket.socket] = []
        self._stream_clients_lock = threading.Lock()
        self._mpegts_bootstrap = bytearray()
        self._bootstrap_reset = threading.Event()

        self._ffmpeg_proc: subprocess.Popen | None = None
        self._ffmpeg_lock = threading.Lock()
        self._video_codec: str | None = None
        self._ffmpeg_reader_thread: threading.Thread | None = None
        self._ffmpeg_stderr_thread: threading.Thread | None = None
        self._video_pacer_thread: threading.Thread | None = None
        self._video_keepalive_thread: threading.Thread | None = None
        self._latest_keyframe: bytes | None = None
        self._video_queue: queue.Queue[tuple[bytes, bool]] = queue.Queue(maxsize=120)

        self._worker_thread: threading.Thread | None = None
        self._idle_watch_thread: threading.Thread | None = None
        self._p2p_streamer = None

        self._stream_profile = STREAM_PROFILE_AUTO
        self._active_video_id = 0
        self._profile_changed = threading.Event()
        self._auto_degraded_windows = 0
        self._auto_substream_failures = 0

        self._metrics_lock = threading.Lock()
        self._stream_state = "idle"
        self._live_switch_enabled = False
        self._reconnect_count = 0
        self._wake_count = 0
        self._last_session_frames = 0
        self._last_session_bytes = 0
        self._last_session_duration = 0.0
        self._last_session_fps = 0.0
        self._last_max_frame_gap = 0.0
        self._pacer_fps = _PACER_MAX_FPS
        self._last_error: str | None = None

        self._last_request_time = 0.0
        self._last_client_time = 0.0
        self._last_video_write = 0.0
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
            return len(self._stream_clients)

    @property
    def stream_source(self) -> str | None:
        """Return the local MPEG-TS source URL."""
        if not self._stream_port:
            return None
        return f"tcp://127.0.0.1:{self._stream_port}"

    @property
    def stream_profile(self) -> str:
        """Return the requested stream profile."""
        return self._stream_profile

    def set_stream_profile(self, profile: str) -> None:
        """Select the stream profile and restart only the active P2P attempt."""
        profile = normalize_stream_profile(profile)
        if profile == self._stream_profile:
            return

        self._stream_profile = profile
        self._active_video_id = 1 if profile == STREAM_PROFILE_SUBSTREAM else 0
        self._auto_degraded_windows = 0
        self._auto_substream_failures = 0
        self._profile_changed.set()
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
            return {
                "stream_profile": self._stream_profile,
                "stream_profile_active": (
                    STREAM_PROFILE_SUBSTREAM
                    if self._active_video_id == 1
                    else STREAM_PROFILE_MAIN
                ),
                "stream_video_id": self._active_video_id,
                "stream_state": self._stream_state,
                "stream_clients": self.client_count,
                "stream_codec": self._video_codec,
                "stream_openapi_host": openapi_host,
                "stream_live_switch": self._live_switch_enabled,
                "stream_reconnects": self._reconnect_count,
                "stream_wakes": self._wake_count,
                "stream_last_frames": self._last_session_frames,
                "stream_last_bytes": self._last_session_bytes,
                "stream_last_duration": round(self._last_session_duration, 2),
                "stream_last_fps": round(self._last_session_fps, 2),
                "stream_last_max_gap": round(self._last_max_frame_gap, 2),
                "stream_pacer_fps": round(self._pacer_fps, 2),
                "stream_last_error": self._last_error,
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

    def stop(self, reason: str = "stopped") -> None:
        """Stop the bridge and free all resources."""
        with self._lock:
            self._stop_locked(reason)

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

        with self._stream_clients_lock:
            for client in self._stream_clients:
                try:
                    client.close()
                except OSError:
                    pass
            self._stream_clients.clear()
            self._mpegts_bootstrap.clear()
            self._bootstrap_reset.clear()

        if self._ffmpeg_proc is not None:
            try:
                if self._ffmpeg_proc.stdin:
                    self._ffmpeg_proc.stdin.close()
            except OSError:
                pass
            try:
                self._ffmpeg_proc.terminate()
                self._ffmpeg_proc.wait(timeout=5)
            except Exception:
                try:
                    self._ffmpeg_proc.kill()
                    self._ffmpeg_proc.wait(timeout=2)
                except Exception:
                    pass
            self._ffmpeg_proc = None

        while not self._video_queue.empty():
            try:
                self._video_queue.get_nowait()
            except queue.Empty:
                break

        if close_listener:
            self._stream_port = 0
        self._worker_thread = None
        self._idle_watch_thread = None
        self._ffmpeg_reader_thread = None
        self._ffmpeg_stderr_thread = None
        self._video_pacer_thread = None
        self._video_keepalive_thread = None
        self._latest_keyframe = None
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
            return True
        except OSError as err:
            _LOGGER.error("Failed to start local stream server for %s: %s", self._serial_number, err)
            self._stream_server = None
            self._stream_port = 0
            return False

    def _accept_stream_clients(self) -> None:
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

            # Per-client failures must never kill the accept loop: a client
            # resetting mid-bootstrap would otherwise leave the port
            # listening but unserviced for every future connection.
            try:
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                client.settimeout(5)
                with self._stream_clients_lock:
                    if self._mpegts_bootstrap:
                        client.sendall(self._mpegts_bootstrap)
                    self._stream_clients.append(client)
            except OSError as err:
                _LOGGER.debug(
                    "Stream client handshake failed for %s: %s", self._serial_number, err
                )
                try:
                    client.close()
                except OSError:
                    pass
                continue

            self._last_client_time = time.monotonic()
            _LOGGER.debug("Stream client connected for %s from %s", self._serial_number, addr)
            if not self._ensure_pipeline_started():
                with self._stream_clients_lock:
                    if client in self._stream_clients:
                        self._stream_clients.remove(client)
                try:
                    client.close()
                except OSError:
                    pass

    def _ensure_pipeline_started(self) -> bool:
        """Start ffmpeg and the P2P worker on the first real stream consumer."""
        with self._lock:
            if self._running:
                return True
            if self._stream_server is None:
                return False

            # HA may ask for stream_source() speculatively. Wake only after its
            # stream worker has opened the local TCP source, which proves there
            # is a real consumer for this camera.
            self._kick_prewake()
            self._running = True
            self._got_keyframe = False
            with self._metrics_lock:
                self._stream_state = "starting"
                self._last_error = None
            self._worker_thread = threading.Thread(
                target=self._stream_worker,
                name=f"cloudedge_stream_{self._serial_number}",
                daemon=True,
            )
            self._worker_thread.start()
            self._idle_watch_thread = threading.Thread(
                target=self._idle_watch_loop,
                name=f"cloudedge_stream_idle_{self._serial_number}",
                daemon=True,
            )
            self._idle_watch_thread.start()

        self._coordinator.notify_stream_state_changed()
        return True

    def _broadcast_stream(self, data: bytes) -> None:
        with self._stream_clients_lock:
            dead: list[socket.socket] = []
            for client in self._stream_clients:
                try:
                    client.sendall(data)
                except (BrokenPipeError, ConnectionError, OSError):
                    dead.append(client)
            for client in dead:
                self._stream_clients.remove(client)
                try:
                    client.close()
                except OSError:
                    pass
            if self._stream_clients:
                self._last_client_time = time.monotonic()

    def _start_ffmpeg_muxer(self, video_codec: str) -> bool:
        with self._ffmpeg_lock:
            if self._ffmpeg_proc is not None and self._ffmpeg_proc.poll() is None:
                return self._video_codec == video_codec
            self._video_codec = video_codec
            return self._start_ffmpeg_muxer_locked(video_codec)

    def _start_ffmpeg_muxer_locked(self, video_codec: str) -> bool:
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
            "0",
            # Stamp every access unit with its real arrival time so media-time
            # tracks wall-clock. The CloudEdge source is a steady but low frame
            # rate (~3-5 fps); forcing a synthetic 15 fps compressed the media
            # clock and made HLS players outrun the producer and stall.
            "-use_wallclock_as_timestamps",
            "1",
            "-thread_queue_size",
            "512",
            "-f",
            video_codec,
            "-i",
            "pipe:0",
            "-an",
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            # Preserve real arrival intervals while removing absolute clock
            # values and invalid decode ordering across held frames/reconnects.
            "-bsf:v",
            "setts=pts=PTS-STARTPTS:dts=PTS-STARTPTS",
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
            "pipe:1",
        ]

        try:
            self._ffmpeg_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            with self._stream_clients_lock:
                self._mpegts_bootstrap.clear()
        except FileNotFoundError:
            _LOGGER.error("ffmpeg not found in PATH; CloudEdge live stream is unavailable")
            self._ffmpeg_proc = None
            return False
        except Exception as err:
            _LOGGER.error("Failed to start ffmpeg for %s: %s", self._serial_number, err)
            self._ffmpeg_proc = None
            return False

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
            args=(proc,),
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
        return True

    def _ffmpeg_stdout_reader(self, proc: subprocess.Popen) -> None:
        try:
            while True:
                if proc.poll() is not None or proc.stdout is None:
                    return
                data = proc.stdout.read1(32768)
                if not data:
                    return
                with self._stream_clients_lock:
                    if self._bootstrap_reset.is_set():
                        self._mpegts_bootstrap.clear()
                        self._bootstrap_reset.clear()
                    if (
                        len(self._mpegts_bootstrap) + len(data)
                        <= _MAX_MPEGTS_BOOTSTRAP_BYTES
                    ):
                        self._mpegts_bootstrap.extend(data)
                self._broadcast_stream(data)
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

    def _video_pacer(self, proc: subprocess.Popen) -> None:
        # KCP can deliver several seconds of video in a short burst after a gap.
        # Writing that burst immediately makes ffmpeg assign compressed wall-clock
        # timestamps, so HA freezes and then fast-forwards. Space writes using the
        # measured source cadence and never accelerate to catch up.
        next_write_at = 0.0
        while True:
            if proc.poll() is not None or proc.stdin is None:
                return
            try:
                data, reset_bootstrap = self._video_queue.get(timeout=0.5)
            except queue.Empty:
                if not self._running:
                    return
                continue

            try:
                now = time.monotonic()
                if next_write_at > now:
                    time.sleep(next_write_at - now)
                if reset_bootstrap:
                    self._bootstrap_reset.set()
                proc.stdin.write(data)
                proc.stdin.flush()
                self._last_video_write = time.monotonic()
                with self._metrics_lock:
                    pacer_fps = self._pacer_fps
                next_write_at = self._last_video_write + (1.0 / pacer_fps)
            except (BrokenPipeError, OSError, ValueError):
                return

    def _video_keepalive(self, proc: subprocess.Popen) -> None:
        # CloudEdge sources stall in two cases that would otherwise make the HLS
        # playlist stop advancing and the player give up: a slow cold-start wake
        # (the camera needs ~5-20s to leave dormancy) and shared/throttled cameras
        # whose live grant ends after ~15-20s before a fresh window re-establishes.
        # Re-feeding the most recent *keyframe* keeps the muxer/playlist advancing
        # with a held frame (decoder-safe: keyframes are self-contained, no broken
        # P-frame references) until real video resumes.
        while True:
            if proc.poll() is not None:
                return
            time.sleep(0.5)
            if not self._running:
                return
            kf = self._latest_keyframe
            if not kf:
                continue
            now = time.monotonic()
            # Bound the hold: if no real video for too long, the camera is likely
            # offline — stop masking it with a frozen frame and let the stream
            # stall so it surfaces as an error.
            if self._last_real_video and now - self._last_real_video > _KEEPALIVE_MAX_HOLD:
                continue
            if now - self._last_video_write > 0.5 and self._video_queue.empty():
                self._enqueue_video(kf)

    def _enqueue_video(self, data: bytes, *, reset_bootstrap: bool = False) -> None:
        try:
            self._video_queue.put_nowait((data, reset_bootstrap))
        except queue.Full:
            _LOGGER.debug("Dropping video frame for %s because ffmpeg is backlogged", self._serial_number)

    def _get_stream_device(self) -> dict | None:
        device = self._coordinator.get_device_for_stream(self._serial_number)
        if device is None:
            _LOGGER.warning("No device data available for live stream %s", self._serial_number)
        return device

    def _set_live_switch(self, device: dict, enabled: bool) -> bool:
        """Set parameter 167 once for the whole HA viewing session."""
        client = self._coordinator.client
        if client is None or not hasattr(client, "set_device_config"):
            return False
        serial_number = device.get("serial_number") or self._serial_number
        try:
            changed = bool(
                client.set_device_config(
                    serial_number,
                    {"167": 1 if enabled else 0},
                    auto_wake=False,
                    device_id=device.get("device_id"),
                )
            )
            if changed:
                with self._metrics_lock:
                    self._live_switch_enabled = enabled
                _LOGGER.debug(
                    "Live-stream switch 167=%d for %s",
                    1 if enabled else 0,
                    serial_number,
                )
            return changed
        except Exception as err:
            _LOGGER.debug(
                "Could not %s live-stream switch for %s: %s",
                "enable" if enabled else "disable",
                serial_number,
                err,
            )
            return False

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

        if video_id == 1:
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
                self._active_video_id = 0
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
        if self._auto_degraded_windows >= _AUTO_FALLBACK_WINDOWS:
            _LOGGER.info(
                "Auto profile switching %s to substream after %d degraded windows "
                "(%.2f fps, %.2fs max gap)",
                self._serial_number,
                self._auto_degraded_windows,
                fps,
                max_frame_gap,
            )
            self._active_video_id = 1
            self._auto_substream_failures = 0

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

    def _stream_worker(self) -> None:
        consecutive_failures = 0
        device: dict | None = None
        bridge_owns_live_switch = False
        attempt_number = 0
        try:
            while self._running and self._should_keep_running():
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

            try:
                create_parameters = inspect.signature(client.create_streamer).parameters
            except (TypeError, ValueError):
                create_parameters = {}
            supports_external_switch = "manage_stream_switch" in create_parameters
            if supports_external_switch:
                bridge_owns_live_switch = self._set_live_switch(device, True)

            while self._running:
                if not self._should_keep_running():
                    return

                client = self._coordinator.client
                device = self._get_stream_device()
                if client is None or device is None:
                    time.sleep(2)
                    continue

                attempt_logged_in = False
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
                self._profile_changed.clear()
                video_id = self._active_video_id
                with self._metrics_lock:
                    self._stream_state = "connecting"
                    if video_id == 1:
                        self._pacer_fps = _SUBSTREAM_INITIAL_FPS

                def on_video(frame_data: bytes) -> None:
                    nonlocal attempt_frames, attempt_bytes
                    nonlocal first_frame_at, last_frame_at, max_frame_gap
                    received_at = time.monotonic()
                    attempt_frames += 1
                    attempt_bytes += len(frame_data)
                    if not first_frame_at:
                        first_frame_at = received_at
                    if last_frame_at:
                        max_frame_gap = max(max_frame_gap, received_at - last_frame_at)
                    last_frame_at = received_at
                    media_span = received_at - first_frame_at
                    if attempt_frames >= 5 and media_span >= 2.0:
                        observed_fps = (attempt_frames - 1) / media_span
                        observed_fps = max(
                            _PACER_MIN_FPS,
                            min(_PACER_MAX_FPS, observed_fps),
                        )
                        with self._metrics_lock:
                            # Smooth enough to avoid oscillating on individual
                            # packets, but react quickly after a multi-second gap.
                            self._pacer_fps = (
                                self._pacer_fps * 0.5 + observed_fps * 0.5
                            )

                    frame_data = _convert_to_annex_b(frame_data)
                    codec = self._video_codec or _detect_video_codec(frame_data)
                    if codec is None:
                        return
                    is_keyframe = _is_video_keyframe(frame_data, codec)
                    if not self._got_keyframe:
                        if not is_keyframe:
                            return
                        if not self._start_ffmpeg_muxer(codec):
                            _LOGGER.error(
                                "Failed to start %s muxer for %s",
                                codec,
                                self._serial_number,
                            )
                            self._running = False
                            return
                        self._got_keyframe = True
                        _LOGGER.info(
                            "First live %s keyframe received for %s (%d bytes)",
                            codec,
                            self._serial_number,
                            len(frame_data),
                        )
                    self._last_real_video = time.monotonic()
                    if is_keyframe:
                        # Remembered for the keepalive: a held frame to re-feed
                        # during wake / live-window gaps so HLS keeps advancing.
                        self._latest_keyframe = frame_data
                    self._enqueue_video(frame_data, reset_bootstrap=is_keyframe)

                def on_login() -> None:
                    nonlocal attempt_logged_in
                    attempt_logged_in = True
                    self._last_online_confirmation = time.monotonic()
                    self._coordinator.set_runtime_connection_status(
                        self._serial_number,
                        "online",
                    )
                    with self._metrics_lock:
                        self._stream_state = "streaming"
                    _LOGGER.info("Live stream connected for %s", self._serial_number)
                    self._coordinator.notify_stream_state_changed()

                def on_disconnect() -> None:
                    with self._metrics_lock:
                        if self._running:
                            self._stream_state = "reconnecting"
                    _LOGGER.info("Live stream disconnected for %s", self._serial_number)
                    self._coordinator.notify_stream_state_changed()

                try:
                    create_kwargs = dict(
                        device=device,
                        on_video=on_video,
                        on_login=on_login,
                        on_disconnect=on_disconnect,
                        video_id=video_id,
                    )
                    if supports_external_switch:
                        create_kwargs["manage_stream_switch"] = not bridge_owns_live_switch
                    self._p2p_streamer = client.create_streamer(**create_kwargs)
                    if self._p2p_streamer is None:
                        _LOGGER.error("Failed to create P2P streamer for %s", self._serial_number)
                        return
                    _LOGGER.debug(
                        "Using CloudEdge video_id=%d for %s",
                        video_id,
                        self._serial_number,
                    )
                    if not getattr(
                        self._p2p_streamer,
                        "_cloudedge_native_signaling_retry",
                        False,
                    ):
                        self._patch_streamer_status_retry(self._p2p_streamer)
                    result = self._p2p_streamer.run_session()
                    if isinstance(result, tuple) and len(result) == 2:
                        attempt_frames = max(attempt_frames, int(result[0]))
                        attempt_bytes = max(attempt_bytes, int(result[1]))
                except Exception as err:
                    with self._metrics_lock:
                        self._last_error = str(err)
                    _LOGGER.error("P2P stream failed for %s: %s", self._serial_number, err)
                finally:
                    self._p2p_streamer = None

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

                # Keep the local listener available for the idle grace period,
                # but do not consume battery by opening another P2P window when
                # Home Assistant no longer has an active MPEG-TS consumer.
                if not self.client_count:
                    return
                if not self._should_keep_running():
                    return

                # A finished window or a cooldown-collided attempt both land here.
                # The camera rate-limits back-to-back sessions, so a single failed
                # attempt is expected — wait the cooldown and re-establish a fresh
                # window rather than giving up. Bail only after repeated failures
                # (camera genuinely offline / asleep).
                if attempt_logged_in:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= _MAX_CONSECUTIVE_STREAM_FAILURES:
                        _LOGGER.warning(
                            "Live stream for %s failed %d times in a row; giving up "
                            "until a new stream request",
                            self._serial_number,
                            consecutive_failures,
                        )
                        return

                if self._profile_changed.is_set():
                    continue

                # Responsive cooldown: bail early if all consumers disappear.
                cooldown_until = time.monotonic() + _RECONNECT_COOLDOWN
                while time.monotonic() < cooldown_until:
                    if (
                        not self._running
                        or not self.client_count
                        or not self._should_keep_running()
                    ):
                        return
                    if self._profile_changed.is_set():
                        break
                    time.sleep(0.5)
        finally:
            if bridge_owns_live_switch and device is not None:
                self._set_live_switch(device, False)
            self.stop("worker_exit")

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

    def _idle_watch_loop(self) -> None:
        while self._running:
            time.sleep(1)
            if self._should_keep_running():
                continue
            _LOGGER.debug("Stopping idle live stream for %s", self._serial_number)
            streamer = self._p2p_streamer
            if streamer is not None:
                try:
                    streamer.request_stop()
                except Exception:
                    pass
            else:
                self.stop("idle_timeout")
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
        return bridge.diagnostics() if bridge else {
            "stream_profile": STREAM_PROFILE_AUTO,
            "stream_profile_active": STREAM_PROFILE_MAIN,
            "stream_state": "idle",
        }

    def stop_all(self) -> None:
        with self._lock:
            bridges = list(self._bridges.values())
        for bridge in bridges:
            bridge.stop("manager_stop")

    def remove_missing(self, serial_numbers: set[str]) -> None:
        with self._lock:
            missing = [sn for sn in self._bridges if sn not in serial_numbers]
            bridges = [self._bridges.pop(sn) for sn in missing]
        for bridge in bridges:
            bridge.stop("device_removed")
