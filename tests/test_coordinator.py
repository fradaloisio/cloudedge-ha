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
        with patch.object(coordinator, "async_set_updated_data") as publish:
            await asyncio.sleep(0)
        assert old_data["sn"]["connection_status"] == "offline"
        assert publish.call_args.args[0]["sn"]["last_motion_time"] == 42
        assert publish.call_args.args[0]["sn"]["connection_status"] == "online"

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
