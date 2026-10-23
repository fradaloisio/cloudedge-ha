"""Bound each local stream consumer without blocking the media producer."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
import logging
import socket
import threading
from time import monotonic

_LOGGER = logging.getLogger(__name__)

# Accept the bridge's maximum bootstrap plus one live chunk. A consumer that
# cannot drain it promptly is disconnected instead of delaying every viewer.
_MAX_PENDING_BYTES = 4 * 1024 * 1024 + 32768
_MAX_PENDING_AGE = 1.0
_SEND_TIMEOUT = 1.0
# The zero-size chunk that ends an HTTP/1.1 chunked response.
_LAST_CHUNK = b"0\r\n\r\n"


class StreamConsumer:
    """Send one ordered byte stream on its own bounded writer thread.

    Register and enqueue the bootstrap before calling start(). The producer may
    enqueue live chunks meanwhile; a single writer preserves their order.
    on_closed is called only by that writer, never synchronously from enqueue,
    finish or close, so the bridge can safely manage consumers under its own
    lock.

    A chunked consumer sends each enqueued block as one HTTP/1.1 chunk, so
    finish() can end the response cleanly. A raw one sends the bytes as they
    are: its stream ends only with the connection.
    """

    def __init__(
        self,
        sock: socket.socket,
        on_closed: Callable[[socket.socket], None],
        *,
        chunked: bool = False,
    ) -> None:
        self._socket = sock
        self._on_closed = on_closed
        self._chunked = chunked
        self._condition = threading.Condition()
        self._queue: deque[tuple[float, bytes]] = deque()
        self._pending_bytes = 0
        self._closed = False
        self._finishing = False
        self._thread: threading.Thread | None = None
        sock.settimeout(_SEND_TIMEOUT)

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def start(self) -> None:
        with self._condition:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._write, name="cloudedge_stream_consumer", daemon=True
            )
            self._thread.start()

    def enqueue(self, data: bytes) -> bool:
        """Accept bytes immediately, or disconnect an overloaded consumer."""
        with self._condition:
            if self._closed or self._finishing:
                return False
            if not data:
                # Never queued: as a chunk it would end the response.
                return True
            now = monotonic()
            too_old = self._queue and now - self._queue[0][0] > _MAX_PENDING_AGE
            if not too_old and self._pending_bytes + len(data) <= _MAX_PENDING_BYTES:
                self._queue.append((now, data))
                self._pending_bytes += len(data)
                self._condition.notify()
                return True
        self.close()
        return False

    def finish(self) -> None:
        """End the stream cleanly, without waiting for the socket.

        The writer sends what is already queued and, for a chunked stream,
        the last chunk, then closes the connection. The client thus reads a
        complete response rather than a connection that broke off, which
        FFmpeg reports as "Stream ends prematurely" and Go's HTTP client
        (go2rtc) as an unexpected EOF. A writer that cannot send in time
        gives up as usual (_SEND_TIMEOUT, _MAX_PENDING_AGE).
        """
        with self._condition:
            self._finishing = True
            self._condition.notify_all()

    def close(self) -> None:
        """Release queued bytes and interrupt a blocked socket write."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._queue.clear()
            self._pending_bytes = 0
            self._condition.notify_all()
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._socket.close()
        except OSError:
            pass

    def _write(self) -> None:
        try:
            while True:
                with self._condition:
                    while not (self._closed or self._finishing or self._queue):
                        self._condition.wait()
                    if self._closed:
                        return
                    if not self._queue:
                        break  # finishing, and everything queued was sent
                    queued_at, data = self._queue.popleft()
                if monotonic() - queued_at > _MAX_PENDING_AGE:
                    return
                if self._chunked:
                    self._socket.sendall(b"%x\r\n%b\r\n" % (len(data), data))
                else:
                    self._socket.sendall(data)
                with self._condition:
                    if not self._closed:
                        self._pending_bytes -= len(data)
            if self._chunked:
                self._socket.sendall(_LAST_CHUNK)
        except OSError:
            pass
        finally:
            self.close()
            try:
                self._on_closed(self._socket)
            except Exception:
                _LOGGER.exception("Failed to unregister a closed stream consumer")
