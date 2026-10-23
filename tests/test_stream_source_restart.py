"""HA must reopen a changed media format before the old TCP source ends."""

import asyncio
import threading
from unittest.mock import Mock, patch

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("av")

from homeassistant.components.camera.prefs import DynamicStreamSettings
from homeassistant.components.stream import Stream
from homeassistant.components.stream.core import STREAM_SETTINGS_NON_LL_HLS
from homeassistant.components.stream.exceptions import StreamEndedError, StreamWorkerError
from homeassistant.core import HomeAssistant

from custom_components.cloudedge import CloudEdgeCoordinator
from custom_components.cloudedge.camera import CloudEdgeCamera


async def camera_with_stream(tmp_path):
    """A subscribed camera entity and its real HA Stream (not started)."""
    hass = HomeAssistant(str(tmp_path))
    with patch("homeassistant.helpers.frame.report_usage"):
        coordinator = CloudEdgeCoordinator(
            hass, "user@example.test", "password", "IT", "+39", 5,
            Mock(entry_id="stream-test"),
        )
    coordinator.data = {"camera": {"name": "Camera", "device_id": "id"}}
    camera = CloudEdgeCamera(coordinator, "camera", coordinator.data["camera"])
    camera.hass = hass
    camera.entity_id = "camera.stream_test"
    await camera.async_added_to_hass()
    stream = Stream(
        hass, "tcp://127.0.0.1:12345", {}, STREAM_SETTINGS_NON_LL_HLS,
        DynamicStreamSettings(),
    )
    camera.stream = stream
    return coordinator, camera, stream


async def release_camera(coordinator, camera):
    # Execute the removal callbacks registered by Entity and Camera; no
    # platform is needed for these stream-worker lifecycle tests.
    for remove in camera._on_remove or ():
        remove()
    await coordinator.async_shutdown()
    await asyncio.sleep(0)


def test_format_change_reopens_ha_worker_without_backoff(tmp_path):
    """Real HA retry loop and camera subscription; only network I/O is replaced."""

    async def run():
        coordinator, camera, stream = await camera_with_stream(tmp_path)
        # HA only runs a worker on behalf of an output (a viewer).
        stream.add_provider("hls")
        opened = threading.Event()
        tcp_closed = threading.Event()
        reopened = threading.Event()
        calls = []
        errors = []

        def receive(*args):
            calls.append(args[0])
            if len(calls) == 1:
                opened.set()
                if not tcp_closed.wait(3):
                    errors.append("TCP source never closed")
                if not stream._thread_quit.is_set():
                    raise StreamWorkerError("Unexpected EOF before format change")
            else:
                reopened.set()
                stream._thread_quit.set()

        worker = threading.Thread(target=stream._run_worker)
        try:
            with patch("homeassistant.components.stream.worker.stream_worker", receive):
                worker.start()
                assert await asyncio.to_thread(opened.wait, 3)
                assert await asyncio.to_thread(
                    coordinator.notify_stream_source_changed, "camera", tcp_closed.set
                )
                assert await asyncio.to_thread(reopened.wait, 3)
                await asyncio.to_thread(worker.join, 3)
                assert not worker.is_alive()
                assert calls == [stream.source, stream.source]
                assert not errors
                assert stream._diagnostics.as_dict().get("worker_error", 0) == 0
                assert stream._diagnostics.as_dict().get("retry_timeout", 0) == 0
        finally:
            stream._thread_quit.set()
            tcp_closed.set()
            await asyncio.to_thread(worker.join, 3)
            await release_camera(coordinator, camera)

    asyncio.run(run())


class BridgeSource:
    """HA's reader of the bridge's TCP source; only the socket I/O is fake.

    One mode per connection: ``data`` streams until HA asks its worker to quit
    or the bridge closes the source; ``read`` is blocked inside a read that
    only the bridge's EOF ends (no video yet); ``error`` fails at once, like
    HA's PyAV read timeout.
    """

    def __init__(self, *modes):
        self.modes = list(modes)
        self.connections = 0
        self.connected = threading.Event()
        self.aborted = threading.Event()
        self._lock = threading.Lock()
        self._open = []

    def close_consumers(self):
        """The bridge's _close_stream_consumers: EOF on every open connection."""
        with self._lock:
            for eof in self._open:
                eof.set()
            self._open.clear()

    def stream_worker(self, source, options, settings, state, converter, quit_event):
        with self._lock:
            self.connections += 1
            mode = self.modes.pop(0) if self.modes else "data"
            eof = threading.Event()
            self._open.append(eof)
        self.connected.set()
        if mode == "error":
            raise StreamWorkerError("Error demuxing stream")
        while not self.aborted.is_set():
            if eof.wait(0.02):
                raise StreamEndedError("Stream ended; no additional packets")
            if mode == "data" and quit_event.is_set():
                return
        raise StreamWorkerError("aborted by test cleanup")


@pytest.mark.parametrize("ha_worker", ["retry-backoff", "stopped", "stopping"])
def test_format_change_without_a_reading_worker_keeps_the_stream_usable(
    tmp_path, ha_worker
):
    """The bridge can switch formats while HA's worker is not reading.

    update_source() there either ended HA's retry backoff for good, or latched
    a fast restart that turned the next stop() into a reconnect without any
    viewer: stop() never returned and every later start() hung on the
    Stream's start/stop lock until Home Assistant restarted.
    """

    async def run():
        coordinator, camera, stream = await camera_with_stream(tmp_path)
        source = BridgeSource(
            *{"retry-backoff": ["error"], "stopping": ["read"]}.get(ha_worker, [])
        )
        pending = []

        async def finishes(awaitable, what):
            task = asyncio.ensure_future(awaitable)
            pending.append(task)
            done, _ = await asyncio.wait({task}, timeout=3)
            assert done, f"{what} never returned"
            task.result()

        async def eventually(predicate):
            for _ in range(150):
                if predicate():
                    return True
                await asyncio.sleep(0.02)
            return False

        async def open_view():
            stream.add_provider("hls")
            await finishes(stream.start(), "start()")

        async def format_change():
            assert await asyncio.to_thread(
                coordinator.notify_stream_source_changed, "camera",
                source.close_consumers,
            )

        try:
            with patch("homeassistant.components.stream.worker.stream_worker",
                       source.stream_worker), \
                 patch("homeassistant.components.stream.STREAM_RESTART_INCREMENT", 1):
                await open_view()
                assert await asyncio.to_thread(source.connected.wait, 3)
                if ha_worker == "retry-backoff":
                    assert await eventually(lambda: not stream.available)
                    await format_change()
                    assert await eventually(lambda: source.connections >= 2), (
                        "HA's retry never reconnected its viewer"
                    )
                    await finishes(stream.remove_provider(stream.outputs()["hls"]),
                                   "stop()")
                elif ha_worker == "stopped":
                    await finishes(stream.remove_provider(stream.outputs()["hls"]),
                                   "stop()")
                    await format_change()
                else:
                    stop = asyncio.ensure_future(
                        stream.remove_provider(stream.outputs()["hls"])
                    )
                    pending.append(stop)
                    await asyncio.sleep(0.2)
                    assert not stop.done(), "the reader should still be blocked"
                    await format_change()
                    await finishes(stop, "pending stop()")
                # The camera stays viewable: the next viewer opens and leaves.
                await open_view()
                await finishes(stream.remove_provider(stream.outputs()["hls"]),
                               "next viewer's stop()")
        finally:
            # On failure HA's worker may be looping with a latched restart:
            # drive it out so no executor join outlives the test.
            source.aborted.set()
            for _ in range(100):
                thread = stream._thread
                if thread is None or not thread.is_alive():
                    break
                stream._fast_restart_once = False
                stream._thread_quit.set()
                await asyncio.to_thread(thread.join, 0.1)
            if pending:
                await asyncio.wait(pending, timeout=5)
            await release_camera(coordinator, camera)

    asyncio.run(run())


def test_format_notification_is_per_camera_and_unsubscribes(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        with patch("homeassistant.helpers.frame.report_usage"):
            coordinator = CloudEdgeCoordinator(
                hass, "user@example.test", "password", "IT", "+39", 5,
                Mock(entry_id="stream-test"),
            )
        events = []
        remove = coordinator.async_add_stream_source_listener(
            "camera-a", lambda: events.append("restart-a")
        )
        coordinator.async_add_stream_source_listener(
            "camera-b", lambda: events.append("restart-b")
        )
        try:
            assert await asyncio.to_thread(
                coordinator.notify_stream_source_changed,
                "camera-a", lambda: events.append("close-a"),
            )
            remove()
            remove()
            assert await asyncio.to_thread(
                coordinator.notify_stream_source_changed,
                "camera-a", lambda: events.append("close-a-again"),
            )
            assert events == ["restart-a", "close-a", "close-a-again"]
        finally:
            await coordinator.async_shutdown()

    asyncio.run(run())


def test_expired_format_notification_is_never_delivered_later(tmp_path):
    """HA's loop was too busy to arm the restart in time, so the bridge keeps
    its old muxer. When the loop catches up, the expired request must do
    nothing: the consumers it would close may belong to a later attempt."""

    async def run():
        hass = HomeAssistant(str(tmp_path))
        with patch("homeassistant.helpers.frame.report_usage"):
            coordinator = CloudEdgeCoordinator(
                hass, "user@example.test", "password", "IT", "+39", 5,
                Mock(entry_id="stream-test"),
            )
        events = []
        coordinator.async_add_stream_source_listener(
            "camera", lambda: events.append("restart")
        )
        answers = []
        media_thread = threading.Thread(
            target=lambda: answers.append(
                coordinator.notify_stream_source_changed(
                    "camera", lambda: events.append("close")
                )
            )
        )
        try:
            media_thread.start()
            # HA's loop stays busy (here: blocked in this join) for longer
            # than the bridge waits for it.
            media_thread.join(30)
            assert answers == [False]
            await asyncio.sleep(0)  # the loop runs what was queued meanwhile
            assert events == []
        finally:
            media_thread.join(30)
            await coordinator.async_shutdown()

    asyncio.run(run())
