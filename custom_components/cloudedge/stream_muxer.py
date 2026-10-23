"""Keep compressed camera frames and their write clock together through FFmpeg."""
from __future__ import annotations

import io
import threading
import time
from collections.abc import Callable
from fractions import Fraction
from typing import BinaryIO

import av

_VIDEO_TIME_BASE = Fraction(1, 90_000)


class TimestampedVideoWriter:
    """Write Annex B access units as timestamped NUT packets.

    Raw H.264/HEVC pipes discard access-unit boundaries and timestamps. NUT
    preserves both, so FFmpeg need not infer a clock from buffered byte reads.
    PTS and DTS coincide, as required by the camera's low-delay video stream.
    """

    def __init__(
        self,
        raw_stream: BinaryIO,
        codec: str,
        first_keyframe: bytes,
        *,
        keyframe_detector: Callable[[bytes], bool],
    ) -> None:
        self._raw_stream = raw_stream
        self._keyframe_detector = keyframe_detector
        self._lock = threading.RLock()
        self._closed = False
        self._origin: float | None = None
        self._last_pts = -1
        self._template = av.open(io.BytesIO(first_keyframe), format=codec)
        self._container = None
        try:
            self._container = av.open(
                raw_stream,
                mode="w",
                format="nut",
                buffer_size=1024,
                options={"flush_packets": "1"},
            )
            self._container.flags |= av.container.Flags.flush_packets.value
            self._stream = self._container.add_stream_from_template(
                self._template.streams.video[0]
            )
            self._stream.time_base = _VIDEO_TIME_BASE
        except Exception:
            try:
                if self._container is not None:
                    self._container.close()
            finally:
                self._template.close()
            raise

    def write(self, data: bytes) -> int:
        """Mux one complete access unit, timestamped at the paced write."""
        with self._lock:
            if self._closed:
                raise ValueError("Video writer is closed")
            now = time.monotonic()
            if self._origin is None:
                self._origin = now
            pts = max(self._last_pts + 1, round((now - self._origin) * 90_000))
            packet = av.Packet(data)
            packet.stream = self._stream
            packet.time_base = _VIDEO_TIME_BASE
            packet.pts = packet.dts = pts
            packet.is_keyframe = self._keyframe_detector(data)
            self._container.mux(packet)
            self._last_pts = pts
            return len(data)

    def flush(self) -> None:
        with self._lock:
            self._raw_stream.flush()

    def close(self) -> None:
        """Release resources after the consumer process has terminated."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._container.close()
            except (BrokenPipeError, OSError):
                # Teardown kills FFmpeg first to release a blocked pipe writer.
                pass
            finally:
                self._template.close()
                self._raw_stream.close()
