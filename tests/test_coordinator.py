"""Coordinator regressions, exercised with Home Assistant's real base class."""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, Mock, call, patch

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from custom_components.cloudedge import CloudEdgeCoordinator, async_unload_entry
from custom_components.cloudedge.const import DOMAIN


def make_coordinator(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    entry = Mock(entry_id="test-entry")
    with patch("homeassistant.helpers.frame.report_usage"):
        coordinator = CloudEdgeCoordinator(
            hass, "user@example.test", "password", "IT", "+39", 5, entry
        )
    coordinator.client = Mock()
    coordinator.client.session_data = {"loginTime": time.time()}
    coordinator._authenticated = True
    return coordinator


def test_refresh_keeps_devices_with_duplicate_names_separate(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        devices = [
            {"serial_number": "sn-a", "device_id": "id-a", "name": "Camera"},
            {"serial_number": "sn-b", "device_id": "id-b", "name": "Camera"},
        ]
        coordinator.client.get_all_devices.return_value = devices
        coordinator.client.get_device_info.return_value = dict(devices[0])
        coordinator.client.get_device_status.side_effect = [
            {"online": True}, {"online": False}
        ]
        coordinator.client.get_device_config.return_value = {}
        coordinator.client.get_device_online_status.return_value = "offline"

        result = coordinator._fetch_data()

        assert result["sn-b"]["device_id"] == "id-b"
        assert result["sn-b"]["serial_number"] == "sn-b"
        assert result["sn-a"]["online"] is True
        assert result["sn-b"]["online"] is False
        coordinator.client.get_all_devices.assert_called_once()
        assert coordinator.client.get_device_status.call_args_list == [
            call("id-a"), call("id-b")
        ]

    asyncio.run(run())


def test_refresh_preserves_push_that_arrives_during_fetch(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        snapshot = {"sn": {"name": "Camera", "connection_status": "offline"}}
        applied = threading.Event()

        def apply_push():
            coordinator.data = {"sn": {"last_motion_time": time.time()}}
            applied.set()

        def fetch():
            coordinator.hass.loop.call_soon_threadsafe(apply_push)
            assert applied.wait(timeout=5)
            return snapshot

        coordinator._fetch_data = fetch
        result = await coordinator._async_update_data()
        assert result["sn"]["last_motion_time"] == coordinator.data["sn"]["last_motion_time"]
        assert result["sn"]["connection_status"] == "online"

    asyncio.run(run())


def test_parameter_write_and_refresh_target_serial_number(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.data = {
            "sn-a": {"device_id": "id-a", "name": "Camera"},
            "sn-b": {"device_id": "id-b", "name": "Camera"},
        }
        coordinator.client.set_device_config.return_value = True
        coordinator.client.get_device_config.return_value = {"iot": {"103": "1"}}
        await coordinator.async_set_device_parameter("sn-b", "103", 1)
        coordinator.client.set_device_config.assert_called_once_with(
            "sn-b", {"103": 1}, device_id="id-b"
        )
        coordinator.client.get_device_config.assert_called_once_with("sn-b")
        assert "configuration" not in coordinator.data["sn-a"]
        assert coordinator.data["sn-b"]["configuration"]["103"]["value"] == "1"

    asyncio.run(run())


@pytest.mark.parametrize("result", [False, RuntimeError("network error")])
def test_failed_parameter_write_raises_ha_error(tmp_path, result):
    from homeassistant.exceptions import HomeAssistantError

    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.data = {"sn": {"name": "Camera", "device_id": "id"}}
        if isinstance(result, Exception):
            coordinator.client.set_device_config.side_effect = result
        else:
            coordinator.client.set_device_config.return_value = result
        with pytest.raises(HomeAssistantError):
            await coordinator.async_set_device_parameter("sn", "103", 1)
        coordinator.client.get_device_config.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["camera", "config_switch", "generic_switch"])
def test_entities_control_their_serial_number(tmp_path, kind):
    from custom_components.cloudedge.camera import CloudEdgeCamera
    from custom_components.cloudedge.switch import CloudEdgeConfigSwitch, CloudEdgeGenericSwitch
    from cloudedge.iot_parameters import get_parameter_code_by_name

    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.async_set_device_parameter = AsyncMock()
        device = {"name": "Camera", "device_id": "id-b"}
        code = "103"
        if kind == "camera":
            entity = CloudEdgeCamera(coordinator, "sn-b", device)
            code = get_parameter_code_by_name("MOTION_DET_ENABLE")
        elif kind == "config_switch":
            entity = CloudEdgeConfigSwitch(coordinator, "sn-b", device, "led_enable", code)
        else:
            entity = CloudEdgeGenericSwitch(coordinator, "sn-b", device, "led_enable", code, {})
        await entity.async_turn_on()
        await entity.async_turn_off()
        assert coordinator.async_set_device_parameter.call_args_list == [
            call("sn-b", code, 1), call("sn-b", code, 0)
        ]

    asyncio.run(run())


def test_refresh_button_targets_its_serial_number(tmp_path):
    from custom_components.cloudedge.button import CloudEdgeRefreshButton

    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.async_refresh_device_config = AsyncMock(return_value=True)
        entity = CloudEdgeRefreshButton(coordinator, "sn-b", {"name": "Camera"})
        entity.async_write_ha_state = Mock()
        await entity.async_press()
        coordinator.async_refresh_device_config.assert_awaited_once_with(
            "Camera", serial_number="sn-b"
        )

    asyncio.run(run())


def test_regular_reauthentication_rotates_mqtt(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        events = []
        coordinator._stop_mqtt = lambda: events.append("stop")
        coordinator._start_mqtt = lambda: events.append("start")
        coordinator.client.authenticate.side_effect = lambda: events.append("login") or True

        assert coordinator._authenticate_client() is True
        assert events == ["stop", "login", "start"]

    asyncio.run(run())


def test_runtime_login_failure_requests_reauthentication(tmp_path):
    from cloudedge.exceptions import AuthenticationError

    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator._fetch_data = Mock(
            side_effect=AuthenticationError("invalid credentials")
        )

        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()

        assert coordinator._authenticated is False

    asyncio.run(run())


def test_fetch_preserves_authentication_failure_type(tmp_path):
    from cloudedge.exceptions import AuthenticationError

    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator._authenticated = False
        coordinator.client.authenticate.return_value = False

        with pytest.raises(AuthenticationError):
            coordinator._fetch_data()

        assert coordinator._force_auth_refresh is True
        # The client owns session replacement under its own synchronization.
        assert coordinator.client.session_data is not None

    asyncio.run(run())


def test_network_error_during_login_is_transient_not_reauth(tmp_path):
    from cloudedge.exceptions import NetworkError

    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator._authenticated = False
        coordinator.client.authenticate.side_effect = NetworkError("connection reset")

        with pytest.raises(UpdateFailed, match="login failed"):
            coordinator._fetch_data()

        # A transient outage must not arm forced re-authentication nor
        # reach the coordinator as ConfigEntryAuthFailed (which would stop
        # polling and ask the user to replace valid credentials).
        assert coordinator._force_auth_refresh is False

    asyncio.run(run())


def test_update_data_maps_network_error_to_update_failed(tmp_path):
    from cloudedge.exceptions import NetworkError

    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator._fetch_data = Mock(side_effect=NetworkError("dns failure"))
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    asyncio.run(run())


def test_set_parameter_for_device_not_yet_polled_in(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.data = {}
        loop_thread = threading.get_ident()
        lookup_threads = []

        def get_inventory():
            lookup_threads.append(threading.get_ident())
            return [
                {"serial_number": "sn-g", "device_id": "id-g", "name": "Garage"}
            ]

        coordinator.client.get_all_devices.side_effect = get_inventory
        coordinator.client.set_device_config.return_value = True

        await coordinator.async_set_device_parameter("sn-g", "103", 1)

        coordinator.client.set_device_config.assert_called_once_with(
            "sn-g", {"103": 1}, device_id="id-g"
        )
        assert len(lookup_threads) == 1
        assert lookup_threads[0] != loop_thread

    asyncio.run(run())


def test_set_parameter_with_unknown_serial_fails_cleanly(tmp_path):
    from homeassistant.exceptions import HomeAssistantError

    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.data = {}
        coordinator.client.get_all_devices.return_value = []

        with pytest.raises(HomeAssistantError, match="not available"):
            await coordinator.async_set_device_parameter("sn-x", "103", 1)

        coordinator.client.set_device_config.assert_not_called()

    asyncio.run(run())


def test_failed_unload_keeps_transports_running(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator._stop_mqtt = Mock()
        coordinator.stop_streams = Mock()
        hass = coordinator.hass
        hass.data[DOMAIN] = {"test-entry": coordinator}
        hass.config_entries = Mock()
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)

        assert await async_unload_entry(hass, coordinator.config_entry) is False
        coordinator._stop_mqtt.assert_not_called()
        coordinator.stop_streams.assert_not_called()
        assert hass.data[DOMAIN]["test-entry"] is coordinator

    asyncio.run(run())


def test_first_refresh_propagates_cancellation(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.async_config_entry_first_refresh = AsyncMock(
            side_effect=asyncio.CancelledError
        )
        with pytest.raises(asyncio.CancelledError):
            await coordinator.async_safe_first_refresh()

    asyncio.run(run())


def test_first_refresh_propagates_authentication_failure(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.async_config_entry_first_refresh = AsyncMock(
            side_effect=ConfigEntryAuthFailed("reauth required")
        )
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator.async_safe_first_refresh()

    asyncio.run(run())


def test_setup_auth_failure_stops_transports(tmp_path):
    from custom_components.cloudedge import async_setup_entry

    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.data = {DOMAIN: {}}
        hass.config_entries = Mock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        entry = Mock(entry_id="test-entry")
        entry.data = {
            "username": "user@example.test",
            "password": "password",
            "country_code": "IT",
            "phone_code": "+39",
        }
        stop_mqtt = Mock()
        stop_streams = Mock()
        with patch("homeassistant.helpers.frame.report_usage"), \
             patch.object(CloudEdgeCoordinator, "async_validate_authentication", AsyncMock()), \
             patch.object(CloudEdgeCoordinator, "_stop_mqtt", stop_mqtt), \
             patch.object(CloudEdgeCoordinator, "stop_streams", stop_streams), \
             patch.object(
                 CloudEdgeCoordinator,
                 "async_config_entry_first_refresh",
                 AsyncMock(side_effect=ConfigEntryAuthFailed("reauth")),
             ), \
             patch("custom_components.cloudedge.async_setup_services", AsyncMock()):
            with pytest.raises(ConfigEntryAuthFailed):
                await async_setup_entry(hass, entry)
        # async_unload_entry never runs for a failed setup: transports must
        # have been stopped here or they would outlive the coordinator.
        stop_mqtt.assert_called()
        stop_streams.assert_called()
        assert entry.entry_id not in hass.data[DOMAIN]

    asyncio.run(run())


def test_home_assistant_stop_stops_live_streams_off_the_event_loop(tmp_path):
    # HA does not unload config entries when it stops, and the bridge threads
    # are daemons: FFmpeg, the P2P session and the audio FIFO outlived every
    # stop or restart.
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP
    from custom_components.cloudedge import async_setup_entry

    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.data = {DOMAIN: {}}
        hass.config_entries = Mock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        entry = Mock(entry_id="test-entry")
        entry.data = {
            "username": "user@example.test",
            "password": "password",
            "country_code": "IT",
            "phone_code": "+39",
        }
        on_unload = []
        entry.async_on_unload = on_unload.append
        stopped_on = []
        mqtt_stopped_on = []

        def stop_streams(self):
            stopped_on.append(threading.current_thread())

        with patch("homeassistant.helpers.frame.report_usage"), \
             patch.object(CloudEdgeCoordinator, "async_validate_authentication", AsyncMock()), \
             patch.object(CloudEdgeCoordinator, "async_safe_first_refresh",
                          AsyncMock(return_value=True)), \
             patch.object(CloudEdgeCoordinator, "stop_streams", stop_streams), \
             patch("custom_components.cloudedge.async_setup_services", AsyncMock()):
            assert await async_setup_entry(hass, entry)
            # Reload: the unloaded coordinator must not be stopped (or kept
            # alive) by a listener left behind. Run the unload callbacks the
            # way ConfigEntry does, awaiting the coroutines they return.
            while on_unload:
                if asyncio.iscoroutine(result := on_unload.pop()()):
                    await result
            assert await async_setup_entry(hass, entry)
            # The MQTT connection also outlived every stop: same treatment.
            coordinator = hass.data[DOMAIN][entry.entry_id]
            listener = coordinator._mqtt_listener = Mock()
            listener.stop.side_effect = (
                lambda: mqtt_stopped_on.append(threading.current_thread())
            )
            hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
            await hass.async_block_till_done()

        assert len(stopped_on) == 1
        # Stopping blocks (FFmpeg teardown, worker joins): never on the loop.
        assert stopped_on[0] is not threading.current_thread()
        assert len(mqtt_stopped_on) == 1
        # stop() joins the paho thread: never on the loop either.
        assert mqtt_stopped_on[0] is not threading.current_thread()
        assert coordinator._mqtt_listener is None
        # Closed for good: a poll still running must not start it again.
        assert coordinator._mqtt_closed

    asyncio.run(run())


def test_unload_tolerates_missing_coordinator(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator._stop_mqtt = Mock()
        coordinator.stop_streams = Mock()
        hass = coordinator.hass
        hass.data[DOMAIN] = {}  # entry already gone from hass.data
        hass.config_entries = Mock()
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

        assert await async_unload_entry(hass, coordinator.config_entry) is True
        coordinator._stop_mqtt.assert_not_called()

    asyncio.run(run())


def test_second_fetch_while_previous_is_running_is_rejected(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.client.get_all_devices.return_value = []
        assert coordinator._fetch_lock.acquire(blocking=False)
        try:
            with pytest.raises(UpdateFailed, match="still in progress"):
                coordinator._fetch_data()
        finally:
            coordinator._fetch_lock.release()
        # Lock released: the next update fetches normally again.
        assert coordinator._fetch_data() == {}

    asyncio.run(run())


def test_update_failed_from_fetch_is_not_rewrapped(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        original = UpdateFailed("cloud down")
        coordinator._fetch_data = Mock(side_effect=original)
        with pytest.raises(UpdateFailed) as excinfo:
            await coordinator._async_update_data()
        assert excinfo.value is original

    asyncio.run(run())


def test_diagnostics_work_before_and_after_refresh(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        assert coordinator.get_coordinator_info()["last_update_time"] is None
        coordinator._fetch_data = Mock(return_value={})
        await coordinator._async_update_data()
        assert coordinator.get_coordinator_info()["last_update_time"] is not None

    asyncio.run(run())


def test_runtime_update_reads_current_data_on_event_loop(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.data = {"sn": {"connection_status": "offline"}}
        old_data = coordinator.data
        coordinator.set_runtime_connection_status("sn", "online")
        # A push arrives before the scheduled stream callback is processed.
        coordinator.data = {"sn": {"connection_status": "offline", "last_motion_time": 42}}
        with patch.object(coordinator, "async_update_listeners") as publish:
            await asyncio.sleep(0)
        publish.assert_called_once()
        assert old_data["sn"]["connection_status"] == "offline"
        assert coordinator.data["sn"]["last_motion_time"] == 42
        assert coordinator.data["sn"]["connection_status"] == "online"

    asyncio.run(run())


def _publish_every_runtime_update(coordinator):
    """Deliver an MQTT push, a stream status change and a stream notice."""
    coordinator._async_handle_push_event("id", "PIR", 1, True, "")
    coordinator.set_runtime_connection_status("sn", "dormancy")
    coordinator.notify_stream_state_changed()


def test_runtime_updates_keep_the_poll_schedule_and_failure_state(tmp_path):
    # Runtime pushes used async_set_updated_data, which postpones the poll by
    # a full interval (frequent pushes then starve polling) and marks a
    # failed poll successful, reviving entities that show stale data.
    from cloudedge.exceptions import NetworkError

    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.config_entry.pref_disable_polling = False
        coordinator.data = {"sn": {"device_id": "id", "name": "Camera"}}
        coordinator._fetch_data = Mock(side_effect=NetworkError("cloud down"))
        updates = []
        unsubscribe = coordinator.async_add_listener(
            lambda: updates.append(coordinator.data["sn"])
        )
        await coordinator.async_refresh()
        assert coordinator.last_update_success is False
        scheduled_poll = coordinator._unsub_refresh
        assert scheduled_poll is not None
        updates.clear()

        _publish_every_runtime_update(coordinator)
        await coordinator.hass.async_block_till_done()

        assert len(updates) == 3
        assert updates[-1]["last_motion_event"] == "PIR"
        assert updates[-1]["connection_status"] == "dormancy"
        assert coordinator._unsub_refresh == scheduled_poll
        assert coordinator.last_update_success is False
        unsubscribe()

    asyncio.run(run())


def test_runtime_updates_do_not_restart_polling_after_auth_failure(tmp_path):
    # After ConfigEntryAuthFailed polling stops until reauth; a runtime push
    # used to reschedule it, retrying the login with rejected credentials.
    from cloudedge.exceptions import AuthenticationError

    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.config_entry.pref_disable_polling = False
        coordinator.data = {"sn": {"device_id": "id", "name": "Camera"}}
        coordinator._fetch_data = Mock(side_effect=AuthenticationError("rejected"))
        unsubscribe = coordinator.async_add_listener(lambda: None)
        await coordinator.async_refresh()
        assert coordinator._unsub_refresh is None

        _publish_every_runtime_update(coordinator)
        await coordinator.hass.async_block_till_done()

        assert coordinator.data["sn"]["last_motion_event"] == "PIR"
        assert coordinator._unsub_refresh is None
        assert coordinator.last_update_success is False
        coordinator._fetch_data.assert_called_once()
        unsubscribe()

    asyncio.run(run())


@pytest.mark.parametrize("recovery", ["available", "missing", "network_error", "legacy"])
def test_poll_recovers_missing_mqtt_without_disrupting_inventory(tmp_path, recovery):
    from cloudedge.exceptions import NetworkError

    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.client.get_all_devices.return_value = []
        config = {"mqtt_host": "mqtt.example.test"}
        coordinator.client.get_mqtt_config.return_value = (
            config if recovery == "legacy" else None
        )

        def recover():
            if recovery == "network_error":
                raise NetworkError("temporarily unavailable")
            if recovery == "available":
                coordinator.client.get_mqtt_config.return_value = config
                return config
            return None

        if recovery == "legacy":
            del coordinator.client.refresh_mqtt_config
        else:
            coordinator.client.refresh_mqtt_config.side_effect = recover

        with patch("cloudedge.mqtt.CloudEdgeMqttListener") as listener_type:
            listener = listener_type.return_value
            listener.start.return_value = True
            assert coordinator._fetch_data() == {}
            if recovery in {"available", "legacy"}:
                assert coordinator._mqtt_listener is listener
                listener.start.assert_called_once()
                # An established listener must not trigger more recovery work.
                assert coordinator._fetch_data() == {}
                listener.start.assert_called_once()
            else:
                assert coordinator._mqtt_listener is None
                listener_type.assert_not_called()
            if recovery != "legacy":
                coordinator.client.refresh_mqtt_config.assert_called_once()

    asyncio.run(run())


def test_poll_running_at_unload_does_not_restart_mqtt(tmp_path):
    # Unload cannot stop a fetch already running in the executor. Its
    # deferred MQTT start brought a listener up on the dismissed coordinator,
    # which then fought the reloaded entry for the broker session (same
    # client id) and received half of the push events.
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.stop_streams = Mock()
        hass = coordinator.hass
        hass.data[DOMAIN] = {"test-entry": coordinator}
        hass.config_entries = Mock()
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        fetching = threading.Event()
        unloaded = threading.Event()

        def inventory_slower_than_unload():
            fetching.set()
            assert unloaded.wait(timeout=5)
            return []

        coordinator.client.get_all_devices.side_effect = inventory_slower_than_unload
        config = {"mqtt_host": "mqtt.example.test"}
        coordinator.client.refresh_mqtt_config.return_value = config
        coordinator.client.get_mqtt_config.return_value = config

        with patch("cloudedge.mqtt.CloudEdgeMqttListener") as listener_type:
            listener_type.return_value.start.return_value = True
            poll = hass.async_add_executor_job(coordinator._fetch_data)
            assert await hass.async_add_executor_job(fetching.wait, 5)
            assert await async_unload_entry(hass, coordinator.config_entry)
            unloaded.set()
            assert await poll == {}

        listener_type.assert_not_called()
        assert coordinator._mqtt_listener is None

    asyncio.run(run())


def test_mqtt_close_during_start_leaves_no_listener_running(tmp_path):
    # Without serialization a close running while start() is in progress
    # finds no listener, and the one assigned just after is never stopped.
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.client.get_mqtt_config.return_value = {"mqtt_host": "mqtt.example.test"}
        starting = threading.Event()
        proceed = threading.Event()

        def start():
            starting.set()
            assert proceed.wait(timeout=5)
            return True

        with patch("cloudedge.mqtt.CloudEdgeMqttListener") as listener_type:
            listener = listener_type.return_value
            listener.start.side_effect = start
            starter = threading.Thread(target=coordinator._start_mqtt)
            starter.start()
            assert starting.wait(timeout=5)
            # Close while start() is still running.
            closer = threading.Thread(target=coordinator._close_mqtt)
            closer.start()
            closer.join(timeout=0.2)
            proceed.set()
            starter.join(timeout=5)
            closer.join(timeout=5)
            assert not starter.is_alive() and not closer.is_alive()
            # The listener being started is stopped too, and nothing starts
            # MQTT again.
            listener.stop.assert_called_once()
            assert coordinator._mqtt_listener is None
            coordinator._start_mqtt()
            listener_type.assert_called_once()

    asyncio.run(run())


@pytest.mark.parametrize("force_refresh", [False, True])
def test_reauthentication_replaces_the_mqtt_listener(tmp_path, force_refresh):
    # Credential rotation stops MQTT without closing it: the listener must
    # come back with the new session.
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.client.get_mqtt_config.return_value = {"mqtt_host": "mqtt.example.test"}
        coordinator.client.authenticate.return_value = True
        old, new = Mock(), Mock()
        old.start.return_value = new.start.return_value = True
        with patch("cloudedge.mqtt.CloudEdgeMqttListener", side_effect=[old, new]):
            coordinator._start_mqtt()
            assert coordinator._mqtt_listener is old
            assert coordinator._authenticate_client(force_refresh=force_refresh)
        old.stop.assert_called_once()
        assert coordinator._mqtt_listener is new

    asyncio.run(run())


def _make_setup_entry():
    from homeassistant.config_entries import ConfigEntryState

    entry = Mock(entry_id="test-entry", state=ConfigEntryState.SETUP_IN_PROGRESS)
    entry.data = {
        "username": "user@example.test",
        "password": "password",
        "country_code": "IT",
        "phone_code": "+39",
    }
    return entry


@pytest.mark.parametrize("failure", ["update_failed", "timeout"])
def test_failed_first_refresh_lets_home_assistant_retry_the_setup(tmp_path, failure):
    # The failure was swallowed: the entry loaded without entities and the
    # next attempt came one refresh interval later (up to 60 min) instead of
    # within Home Assistant's setup retry backoff.
    from homeassistant.exceptions import ConfigEntryNotReady
    from custom_components.cloudedge import async_setup_entry

    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.data = {DOMAIN: {}}
        hass.config_entries = Mock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        entry = _make_setup_entry()
        close_mqtt = Mock()
        stop_streams = Mock()
        if failure == "update_failed":
            # The real first refresh, failing like a network outage does.
            first_refresh = patch.object(
                CloudEdgeCoordinator, "_fetch_data",
                Mock(side_effect=UpdateFailed("cloud unreachable")),
            )
        else:
            first_refresh = patch.object(
                CloudEdgeCoordinator, "async_config_entry_first_refresh",
                AsyncMock(side_effect=asyncio.TimeoutError),
            )
        with patch("homeassistant.helpers.frame.report_usage"), \
             patch.object(CloudEdgeCoordinator, "async_validate_authentication", AsyncMock()), \
             patch.object(CloudEdgeCoordinator, "_close_mqtt", close_mqtt), \
             patch.object(CloudEdgeCoordinator, "stop_streams", stop_streams), \
             first_refresh, \
             patch("custom_components.cloudedge.async_setup_services", AsyncMock()) as services:
            with pytest.raises(ConfigEntryNotReady):
                await async_setup_entry(hass, entry)
        hass.config_entries.async_forward_entry_setups.assert_not_awaited()
        services.assert_not_awaited()
        assert entry.entry_id not in hass.data[DOMAIN]
        close_mqtt.assert_called()
        stop_streams.assert_called()

    asyncio.run(run())


def test_fetch_abandoned_by_a_failed_setup_does_not_start_mqtt(tmp_path):
    # A failed first refresh fails the setup, which cannot stop a fetch that
    # the update timeout abandoned in the executor. Once that fetch finished,
    # its deferred MQTT start brought a listener up on the dismissed
    # coordinator, which fought the retried setup's listener for the broker
    # session (same client id).
    from homeassistant.exceptions import ConfigEntryNotReady
    from custom_components.cloudedge import async_setup_entry

    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.data = {DOMAIN: {}}
        hass.config_entries = Mock()
        entry = _make_setup_entry()
        fetching = threading.Event()
        setup_failed = threading.Event()
        abandoned = []

        def inventory_slower_than_the_setup():
            fetching.set()
            assert setup_failed.wait(timeout=5)
            return []

        async def first_refresh_times_out(self):
            config = {"mqtt_host": "mqtt.example.test"}
            self.client = Mock(session_data={"loginTime": time.time()})
            self.client.get_all_devices.side_effect = inventory_slower_than_the_setup
            self.client.refresh_mqtt_config.return_value = config
            self.client.get_mqtt_config.return_value = config
            self._authenticated = True
            # The update timeout abandons this executor job: it keeps running.
            abandoned.append((self, hass.async_add_executor_job(self._fetch_data)))
            assert await hass.async_add_executor_job(fetching.wait, 5)
            raise ConfigEntryNotReady("Initial CloudEdge data fetch timed out")

        with patch("homeassistant.helpers.frame.report_usage"), \
             patch.object(CloudEdgeCoordinator, "async_validate_authentication", AsyncMock()), \
             patch.object(CloudEdgeCoordinator, "async_config_entry_first_refresh",
                          first_refresh_times_out), \
             patch("cloudedge.mqtt.CloudEdgeMqttListener") as listener_type:
            listener_type.return_value.start.return_value = True
            with pytest.raises(ConfigEntryNotReady):
                await async_setup_entry(hass, entry)
            setup_failed.set()
            coordinator, poll = abandoned[0]
            assert await poll == {}

        listener_type.assert_not_called()
        assert coordinator._mqtt_listener is None

    asyncio.run(run())


def test_sleeping_camera_stays_dormant_on_every_poll(tmp_path):
    # The OpenAPI 'dormancy' state used to fall through to the legacy online
    # flag of the previous poll, False for every sleeping camera: Connection
    # Status read 'offline' from the second poll on.
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.client.get_all_devices.return_value = [
            {"serial_number": "sn", "device_id": "id", "name": "Camera"}
        ]
        coordinator.client.get_device_status.return_value = {"online": False}
        coordinator.client.get_device_config.return_value = {}
        coordinator.client.get_device_online_status.return_value = "dormancy"
        coordinator.client.refresh_mqtt_config.return_value = None

        statuses = []
        for _ in range(2):
            coordinator.data = coordinator._fetch_data()
            statuses.append(coordinator.data["sn"]["connection_status"])

        assert statuses == ["dormancy", "dormancy"]

    asyncio.run(run())


def test_coordinator_client_does_not_ping_cameras(tmp_path):
    # A failed LAN ping overrode the cloud state of sleeping cameras, and
    # every poll and stream start paid a config read and a ping per device.
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.client = None
        with patch("cloudedge.CloudEdgeClient") as client_type:
            client = client_type.return_value
            client.session_data = {"loginTime": time.time()}
            client.get_all_devices.return_value = []
            client.get_mqtt_config.return_value = None
            client.refresh_mqtt_config.return_value = None
            await coordinator.async_validate_authentication()
            coordinator.client = None
            assert coordinator._fetch_data() == {}

        assert client_type.call_count == 2
        for created in client_type.call_args_list:
            assert created.kwargs["enable_network_ping"] is False

    asyncio.run(run())
