"""Real media regressions at the camera-to-FFmpeg boundary.

Low motion produces very small P frames: raw byte reads can merge their
timestamps or stall video while AAC continues. Neither transport doubles nor
command-string assertions can detect that failure. These tests use real codecs,
the bridge's pacer/muxer, and Home Assistant's timestamp validator or its
stream worker.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import shutil
import socket
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

av = pytest.importorskip("av")
pytest.importorskip("homeassistant")

from homeassistant.components.camera.prefs import DynamicStreamSettings
from homeassistant.components.stream import Stream
from homeassistant.components.stream import worker as ha_stream_worker
from homeassistant.components.stream.core import STREAM_SETTINGS_NON_LL_HLS
from homeassistant.components.stream.worker import TimestampValidator
from homeassistant.core import HomeAssistant
from custom_components.cloudedge import stream_bridge as bridge_module
from custom_components.cloudedge.stream_bridge import (
    CloudEdgeStreamBridge,
    _extract_parameter_sets,
)


@pytest.fixture(params=["h264", "hevc"])
def video_units(request, tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg is required for the media boundary regression")
    codec = request.param
    encoder = "libx264" if codec == "h264" else "libx265"
    params = "aud=1:keyint=10:min-keyint=10:scenecut=0:bframes=0:ref=1"
    if codec == "hevc":
        params += ":open-gop=0:pools=1:frame-threads=1:log-level=error"
    path = tmp_path / f"source.{codec}"
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "color=c=black:s=64x64:r=5", "-frames:v", "80",
            "-c:v", encoder, "-x264-params" if codec == "h264" else "-x265-params",
            params, "-f", codec, str(path),
        ],
        check=True, capture_output=True, timeout=20,
    )
    data = path.read_bytes()
    header = b"\x09" if codec == "h264" else b"\x46\x01"
    positions = [
        match.start()
        for match in re.finditer(b"\x00\x00(?:\x00)?\x01" + header, data)
    ]
    units = [
        data[start:positions[index + 1] if index + 1 < len(positions) else len(data)]
        for index, start in enumerate(positions)
    ]
    assert len(units) == 80, "The encoder must deliver the requested source frames"
    return codec, units


class ScriptedCamera:
    """Replace only the P2P SDK: the bridge's real worker receives every
    access unit through its on_video callback, so the keyframe gate decides
    when a muxer starts, as it does for a camera."""

    def __init__(self, script):
        self.script = script  # script(on_video), run inside the SDK session
        self.delivered = threading.Event()
        self.stopped = threading.Event()
        self.errors: list[BaseException] = []

    def create_streamer(self, device, on_video, on_audio, on_login, on_disconnect,
                        video_id, manage_stream_switch=True):
        def run_session():
            on_login()
            try:
                self.script(on_video)
            except BaseException as err:  # reported by the test thread
                self.errors.append(err)
            finally:
                self.delivered.set()
            self.stopped.wait(60)  # the session lasts until the bridge stops it
            return 0, 0

        return SimpleNamespace(
            _cloudedge_native_signaling_retry=True,
            run_session=run_session,
            request_stop=self.stopped.set,
        )

    def coordinator(self, **extra):
        return SimpleNamespace(
            client=SimpleNamespace(create_streamer=self.create_streamer),
            get_device_for_stream=lambda serial: {
                "serial_number": serial, "device_id": serial, "type_id": 1,
            },
            notify_stream_state_changed=lambda: None,
            set_runtime_connection_status=lambda serial, status: None,
            **extra,
        )


@pytest.mark.parametrize("restart", [False, True], ids=["steady", "muxer-restart"])
def test_muxer_delivers_continuous_realtime_video_and_audio(
    video_units, restart, tmp_path, caplog, monkeypatch
):
    caplog.set_level(logging.DEBUG, logger="custom_components.cloudedge.stream_bridge")
    codec, units = video_units
    fed = 0
    killed: list[subprocess.Popen] = []
    replacement: list[subprocess.Popen | None] = []

    def camera_feed(on_video):
        nonlocal fed
        next_write = time.monotonic()
        for index, unit in enumerate(units[:60]):
            if restart and index == 30:
                # FFmpeg dies on its own mid-stream (crash, OOM kill). Let it
                # publish what it has; the bridge must then start a new muxer
                # on the same HA socket at the camera's next IDR, which
                # arrives through on_video like every other frame.
                time.sleep(0.4)
                proc = bridge._ffmpeg_proc
                proc.kill()
                proc.wait(timeout=3)
                killed.append(proc)
                deadline = time.monotonic() + 3
                while bridge._got_keyframe and time.monotonic() < deadline:
                    time.sleep(0.02)
                next_write = time.monotonic()
            on_video(unit)
            fed += 1
            next_write += 0.2
            time.sleep(max(0.0, next_write - time.monotonic()))
        replacement.append(bridge._ffmpeg_proc)

    camera = ScriptedCamera(camera_feed)
    bridge = CloudEdgeStreamBridge(camera.coordinator(), f"media-{codec}")
    monkeypatch.setattr(bridge, "_wait_for_prewake", lambda: True)
    chunks: list[bytes] = []
    first_output = threading.Event()
    first_output_at: list[float] = []

    tx, rx = socket.socketpair()
    rx.settimeout(0.5)

    def consume():
        while True:
            try:
                data = rx.recv(32768)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            chunks.append(data)
            if not first_output.is_set():
                first_output_at.append(time.monotonic())
                first_output.set()

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    if hasattr(bridge, "_register_stream_client"):
        assert bridge._register_stream_client(tx)
    else:
        with bridge._stream_clients_lock:
            bridge._stream_clients.append(tx)
    bridge._running = True
    bridge._session_generation = 1
    bridge._pacer_fps = 5.0
    worker = threading.Thread(target=bridge._stream_worker, args=(1,), daemon=True)

    started = time.monotonic()
    try:
        worker.start()
        assert camera.delivered.wait(60), "the camera never finished its frames"
        time.sleep(0.5)
    finally:
        bridge.stop("manager_stop")
        camera.stopped.set()
        worker.join(5)
        tx.close()
        reader.join(timeout=2)
        rx.close()
        reader.join(timeout=1)

    assert not camera.errors, camera.errors
    if restart:
        assert replacement[0] is not None and replacement[0] is not killed[0], (
            "No muxer replaced the FFmpeg process that died"
        )
    assert first_output_at, "The live source must produce MPEG-TS"
    if not restart:
        assert first_output_at[0] - started < 3.5, "Initial probing stalled the live source"
    media = b"".join(chunks)
    (tmp_path / "delivered.ts").write_bytes(media)
    with av.open(io.BytesIO(media)) as container:
        video = container.streams.video[0]
        audio = container.streams.audio[0]
        assert (video.codec_context.name, video.width, video.height) == (codec, 64, 64)
        assert audio.codec_context.name == "aac"
        assert audio.codec_context.profile == "LC"
        assert (audio.codec_context.sample_rate, audio.codec_context.channels) == (8000, 1)
        validator = TimestampValidator(int(1 / video.time_base), int(1 / audio.time_base))
        pts = {"video": [], "audio": []}
        for packet in container.demux(video, audio):
            if not packet.size:
                continue
            assert validator.is_valid(packet), "HA rejected the packet's timestamp"
            assert packet.pts is not None
            pts[packet.stream.type].append(float(packet.pts * packet.time_base))

    (tmp_path / "metrics.json").write_text(json.dumps({
        "codec": codec,
        "restart": restart,
        "first_ts_seconds": first_output_at[0] - started,
        "fed": fed,
        "video_packets": len(pts["video"]),
        "audio_packets": len(pts["audio"]),
        "video_pts": pts["video"],
        "audio_pts": pts["audio"],
        "end_drift_seconds": abs(pts["video"][-1] - pts["audio"][-1]),
    }, indent=2))
    assert len(pts["video"]) >= fed * 0.9, "AAC advanced while video was dropped or held"
    assert pts["audio"][-1] - pts["audio"][0] >= 8.0
    assert pts["video"][-1] - pts["video"][0] >= 8.0
    assert abs(pts["video"][-1] - pts["audio"][-1]) < 0.5
    with av.open(io.BytesIO(media)) as container:
        decoded = sum(1 for _ in container.decode(video=0))
    assert decoded == len(pts["video"]), "The published video packets must all decode"


@pytest.mark.parametrize("video_units", ["h264"], indirect=True)
@pytest.mark.parametrize("retire", ["stop", "format-change"])
def test_idle_muxer_is_retired_without_waiting_for_sigterm(video_units, retire):
    """Retiring FFmpeg holds the bridge's locks; a format change does it on the
    P2P receive thread. After a pause in the input, FFmpeg's NUT demuxer waits
    in read(stdin) and SIGTERM cannot finish: a graceful stop cost the whole
    5 s wait plus SIGKILL, freezing video, KCP ACKs and every unload."""
    codec, units = video_units
    coordinator = SimpleNamespace(
        client=None,
        notify_stream_state_changed=lambda: None,
        notify_stream_source_changed=lambda serial, close: (close(), True)[1],
    )
    bridge = CloudEdgeStreamBridge(coordinator, f"retire-{codec}")
    tx, rx = socket.socketpair()
    reader = threading.Thread(
        target=lambda: [None for _ in iter(lambda: rx.recv(65536), b"")],
        daemon=True,
    )
    reader.start()
    assert bridge._register_stream_client(tx)
    bridge._running = True
    bridge._pacer_fps = 10.0
    parameter_sets = _extract_parameter_sets(units[0], codec)
    proc = None
    try:
        assert bridge._start_ffmpeg_muxer(
            codec, parameter_sets=parameter_sets, first_keyframe=units[0]
        )
        proc = bridge._ffmpeg_proc
        for index, unit in enumerate(units[:10]):
            bridge._enqueue_video(unit, reset_bootstrap=index == 0)
            time.sleep(0.1)
        time.sleep(1.0)  # no input: FFmpeg now blocks reading its stdin

        started = time.monotonic()
        if retire == "stop":
            bridge.stop("manager_stop")
        else:
            # A profile switch delivers new parameter sets mid-stream.
            assert bridge._start_ffmpeg_muxer(
                codec,
                parameter_sets=parameter_sets + b"\x00\x00\x00\x01\x68\xee",
                first_keyframe=units[0],
            )
        elapsed = time.monotonic() - started
        assert proc.poll() is not None, "the retired FFmpeg is still running"
    finally:
        bridge.stop("manager_stop")
        tx.close()
        reader.join(timeout=2)
        rx.close()
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    assert elapsed < 1.0, f"retiring the idle muxer took {elapsed:.2f}s"


@pytest.mark.parametrize("video_units", ["h264"], indirect=True)
def test_video_gap_longer_than_ha_source_timeout_keeps_ha_stream_running(
    video_units, tmp_path, monkeypatch
):
    """A battery camera's reconnect leaves the source about 70 s without
    video. Without data HA's stream worker fails after SOURCE_TIMEOUT (30 s),
    then its reconnect fails on a jump above MAX_TIMESTAMP_GAP (30 s): an
    error, a backoff and a hole in HLS at every live window. Both HA limits are
    scaled to 3 s; the bridge re-feeds its held IDR after 0.5 s, every 0.5 s.
    Real FFmpeg, bridge TCP source and P2P worker, and HA stream worker."""
    codec, units = video_units
    monkeypatch.setattr(ha_stream_worker, "SOURCE_TIMEOUT", 3)
    monkeypatch.setattr(ha_stream_worker, "MAX_TIMESTAMP_GAP", 3)
    monkeypatch.setattr("homeassistant.components.stream.STREAM_RESTART_INCREMENT", 1)
    monkeypatch.setattr(bridge_module, "_KEEPALIVE_GAP", 0.5, raising=False)
    monkeypatch.setattr(bridge_module, "_KEEPALIVE_INTERVAL", 0.5, raising=False)
    video_packets: list[tuple[float, float]] = []  # (arrival, dts seconds)
    mux_packet = ha_stream_worker.StreamMuxer.mux_packet

    def record_packet(muxer, packet):
        if packet.stream.type == "video":
            video_packets.append(
                (time.monotonic(), float(packet.dts * packet.time_base))
            )
        mux_packet(muxer, packet)

    monkeypatch.setattr(ha_stream_worker.StreamMuxer, "mux_packet", record_packet)
    moments: dict[str, float] = {}

    def camera_feed(on_video):
        # 15 fps like the cameras: HA's open analyses 20 video frames, which
        # must fit in its scaled 3 s budget. GOPs of 10, repeated in order.
        next_at = time.monotonic()
        for index in [*range(40), *range(45, 90)]:
            if index == 45:
                # Silent for over twice HA's source timeout, then back in the
                # middle of a GOP: 45-49 refer to frames that never came.
                time.sleep(7.0)
                next_at = time.monotonic()
            if index == 50:  # the camera's next IDR
                moments["idr"] = time.monotonic()
            on_video(units[index % len(units)])
            next_at += 1 / 15
            time.sleep(max(0.0, next_at - time.monotonic()))

    camera = ScriptedCamera(camera_feed)
    bridge = CloudEdgeStreamBridge(camera.coordinator(), f"gap-{codec}")
    monkeypatch.setattr(bridge, "_kick_prewake", lambda: None)
    monkeypatch.setattr(bridge, "_wait_for_prewake", lambda: True)

    async def run():
        hass = HomeAssistant(str(tmp_path))
        source = await hass.async_add_executor_job(bridge.ensure_started)
        stream = Stream(
            hass, source, {}, STREAM_SETTINGS_NON_LL_HLS, DynamicStreamSettings()
        )
        try:
            stream.add_provider("hls")
            await stream.start()
            assert await asyncio.to_thread(camera.delivered.wait, 60)
            await asyncio.sleep(1.0)  # the last frames reach HA
            return stream._diagnostics.as_dict(), stream.available
        finally:
            await stream.stop()
            await hass.async_add_executor_job(bridge.stop, "manager_stop")
            camera.stopped.set()
            await asyncio.sleep(0)

    diagnostics, available = asyncio.run(run())

    assert not camera.errors, camera.errors
    assert diagnostics.get("worker_error", 0) == 0, diagnostics
    assert diagnostics.get("start_worker") == 1, "HA's worker reconnected"
    assert available
    jumps = [later[1] - earlier[1] for earlier, later in zip(video_packets, video_packets[1:])]
    assert max(jumps) < 2.5, f"video timestamps jumped {max(jumps):.2f}s"
    # Video resumed with the camera's next IDR: HA received the frames from
    # there on, and the bridge refused only the 5 frames predicted before it.
    assert len([at for at, _ in video_packets if at >= moments["idr"]]) >= 32
    assert bridge.diagnostics()["stream_dropped_frames"] == 5
