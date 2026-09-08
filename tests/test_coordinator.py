"""Coordinator regressions, exercised with Home Assistant's real base class."""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, Mock, call, patch

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import HomeAssistant
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
