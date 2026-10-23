"""Bound live video latency without handing a decoder an incomplete GOP."""
from __future__ import annotations

from collections import deque
import queue
import threading
import time


class VideoBuffer:
    """A bounded queue which resumes at an IDR after dropping stale video.

    Dropping individual P frames corrupts every following reference frame.
    On overload the entire pending GOP is retired, then admission remains
    closed until a fresh keyframe supplies a complete decoder entry point.
    """

    def __init__(self, max_age: float = 1.0, max_bytes: int = 4 * 1024 * 1024):
        self._max_age = max_age
        self._max_bytes = max_bytes
        self._condition = threading.Condition()
        self._items: deque[tuple[float, bytes, bool]] = deque()
        self._bytes = 0
        self._waiting_for_keyframe = True

    def _retire_locked(self) -> None:
        self._items.clear()
        self._bytes = 0
        self._waiting_for_keyframe = True

    def _expire_locked(self, now: float) -> None:
        if not self._items or now - self._items[0][0] <= self._max_age:
            return
        # A keyframe queued behind the stale head is a complete entry point:
        # resume from the oldest one still within max_age instead of waiting
        # for the camera's next IDR.
        fresh_keyframe = None
        for index in range(len(self._items) - 1, -1, -1):
            queued_at, _, keyframe = self._items[index]
            if now - queued_at > self._max_age:
                break
            if keyframe:
                fresh_keyframe = index
        if fresh_keyframe is None:
            self._retire_locked()
            return
        for _ in range(fresh_keyframe):
            _, data, _ = self._items.popleft()
            self._bytes -= len(data)

    def put(self, data: bytes, keyframe: bool) -> bool:
        with self._condition:
            now = time.monotonic()
            self._expire_locked(now)
            if self._bytes + len(data) > self._max_bytes:
                self._retire_locked()
            if len(data) > self._max_bytes:
                return False
            if self._waiting_for_keyframe and not keyframe:
                return False
            if keyframe:
                self._waiting_for_keyframe = False
            self._items.append((now, data, keyframe))
            self._bytes += len(data)
            self._condition.notify()
            return True

    def get(self, timeout: float = 0.0) -> tuple[bytes, bool]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                self._expire_locked(time.monotonic())
                if self._items:
                    _, data, keyframe = self._items.popleft()
                    self._bytes -= len(data)
                    return data, keyframe
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)

    def require_keyframe_if_idle(self) -> bool:
        """Refuse predicted frames until a fresh keyframe, if none is queued.

        Call it before a keyframe the queue did not supply reaches the
        decoder: predicted frames received afterwards refer to pictures that
        keyframe replaced. Return False, leaving admission unchanged, when a
        frame is already queued.
        """
        with self._condition:
            if self._items:
                return False
            self._waiting_for_keyframe = True
            return True

    def qsize(self) -> int:
        with self._condition:
            return len(self._items)

    def get_nowait(self) -> tuple[bytes, bool]:
        return self.get()

    def empty(self) -> bool:
        with self._condition:
            self._expire_locked(time.monotonic())
            return not self._items

    def clear(self) -> None:
        with self._condition:
            self._retire_locked()
