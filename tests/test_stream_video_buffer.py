"""VideoBuffer bounds latency without handing FFmpeg an incomplete GOP."""
import importlib.util
import queue
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "cloudedge"
    / "stream_video_buffer.py"
)
SPEC = importlib.util.spec_from_file_location("stream_video_buffer", MODULE_PATH)
BUFFER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUFFER)


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(BUFFER.time, "monotonic", lambda: now[0])
    return now


def drain(buffer):
    frames = []
    while True:
        try:
            frames.append(buffer.get_nowait())
        except queue.Empty:
            return frames


def test_after_an_age_retirement_only_a_fresh_idr_restarts_admission(clock):
    # A predicted frame references everything since its IDR: once stale video
    # is dropped, admitting P frames again would corrupt the decoder until
    # the next keyframe.
    buffer = BUFFER.VideoBuffer(max_age=1.0)
    assert not buffer.put(b"p-before-any-idr", False)
    assert buffer.put(b"idr-1", True)
    clock[0] += 0.5
    assert buffer.put(b"p-1", False)
    clock[0] += 0.6  # the queued GOP is now older than max_age
    assert not buffer.put(b"p-2", False)
    clock[0] += 0.1
    assert not buffer.put(b"p-3", False)
    assert buffer.put(b"idr-2", True)
    assert buffer.put(b"p-4", False)

    assert drain(buffer) == [(b"idr-2", True), (b"p-4", False)]


def test_expiry_keeps_a_fresh_keyframe_already_queued_behind_the_stale_head(clock):
    # After a KCP burst the head of the queue can be stale while the next IDR
    # is already queued and fresh. Resume from that IDR: retiring it too
    # froze the video for one more GOP.
    buffer = BUFFER.VideoBuffer(max_age=1.0, max_bytes=100)
    assert buffer.put(b"I" * 30, True)
    clock[0] += 0.5
    assert buffer.put(b"P" * 30, False)
    clock[0] += 0.4
    assert buffer.put(b"J" * 30, True)
    clock[0] += 0.2  # the head is now 1.1 s old, the second IDR 0.2 s
    # Admitted without waiting for another IDR, and within the byte budget
    # only if the dropped head no longer counts.
    assert buffer.put(b"Q" * 30, False)
    assert buffer.put(b"R" * 30, False)

    assert drain(buffer) == [
        (b"J" * 30, True), (b"Q" * 30, False), (b"R" * 30, False),
    ]


def test_expiry_resumes_at_the_oldest_fresh_keyframe(clock):
    # A burst can queue several fresh IDRs behind a stale head: keep them all.
    buffer = BUFFER.VideoBuffer(max_age=1.0)
    for step, data, keyframe in [
        (0.0, b"I0", True), (0.3, b"p1", False), (0.2, b"K1", True),
        (0.1, b"p2", False), (0.2, b"K2", True), (0.1, b"p3", False),
    ]:
        clock[0] += step
        assert buffer.put(data, keyframe)
    clock[0] += 0.15  # I0 is stale; K1 (0.55 s old) and K2 (0.25 s) are fresh
    assert buffer.put(b"q", False)

    assert drain(buffer) == [
        (b"K1", True), (b"p2", False), (b"K2", True), (b"p3", False), (b"q", False),
    ]


def test_keyframe_from_outside_the_queue_closes_admission_only_when_idle(clock):
    # The pacer re-feeds its held IDR in a video gap. A frame queued in the
    # meantime means the camera is back: nothing may jump ahead of it.
    buffer = BUFFER.VideoBuffer(max_age=1.0)
    assert buffer.put(b"idr-1", True)
    assert buffer.put(b"p-1", False)
    assert not buffer.require_keyframe_if_idle()
    assert drain(buffer) == [(b"idr-1", True), (b"p-1", False)]
    # Idle: predicted frames now wait for the camera's next IDR.
    assert buffer.require_keyframe_if_idle()
    assert not buffer.put(b"p-2", False)
    assert buffer.put(b"idr-2", True)
    assert buffer.put(b"p-3", False)

    assert drain(buffer) == [(b"idr-2", True), (b"p-3", False)]


def test_byte_budget_retires_the_whole_gop_and_refuses_oversized_frames(clock):
    buffer = BUFFER.VideoBuffer(max_age=60.0, max_bytes=100)
    assert buffer.put(b"I" * 40, True)
    assert buffer.put(b"P" * 40, False)
    # The next frame would exceed the budget: the queued GOP goes, not just
    # one frame, and the frame itself has no IDR to decode from.
    assert not buffer.put(b"Q" * 40, False)
    assert drain(buffer) == []
    # A frame larger than the whole budget is refused, keyframe or not.
    assert not buffer.put(b"J" * 101, True)
    assert not buffer.put(b"R" * 10, False)
    assert buffer.put(b"K" * 40, True)

    assert drain(buffer) == [(b"K" * 40, True)]
