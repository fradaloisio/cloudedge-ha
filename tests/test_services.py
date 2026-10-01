"""Service handlers target devices safely: local-first, duplicate-name safe."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import HomeAssistantError

from custom_components.cloudedge import services
from custom_components.cloudedge.const import DOMAIN

from test_coordinator import make_coordinator


def make_hass(coordinator):
    hass = Mock()
    hass.data = {DOMAIN: {"test-entry": coordinator}}
    hass.services = Mock()
    hass.services.has_service = Mock(return_value=False)
    hass.config_entries = Mock()
    hass.config_entries.async_entries.return_value = []
    hass.async_add_executor_job = AsyncMock(
        side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs)
    )
    return hass


async def registered_handlers(hass):
    await services.async_setup_services(hass)
    return {
        call.args[1]: call.args[2]
        for call in hass.services.async_register.call_args_list
    }


def test_set_parameter_targets_device_id_with_duplicate_names(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.data = {
            "sn-a": {"name": "Camera", "device_id": "id-a", "serial_number": "sn-a"},
            "sn-b": {"name": "Camera", "device_id": "id-b", "serial_number": "sn-b"},
        }
        coordinator.async_set_device_parameter = AsyncMock()
        coordinator.async_request_refresh = AsyncMock()
        hass = make_hass(coordinator)
        handlers = await registered_handlers(hass)

        await handlers["set_parameter"](Mock(data={
            "device_id": "id-b",
            "parameter_name": "FRONT_LIGHT_SWITCH",
            "value": "1",
        }))

        # The write must target the exact device by serial number, carrying
        # the resolved identity so duplicate names cannot redirect it.
        coordinator.async_set_device_parameter.assert_awaited_once_with(
            "sn-b", "167", "1",
            device={"name": "Camera", "device_id": "id-b", "serial_number": "sn-b"},
        )
        coordinator.async_request_refresh.assert_awaited_once()
        # Local resolution: no cloud lookup was needed.
        coordinator.client.find_device_by_name.assert_not_called()

    asyncio.run(run())


def test_set_parameter_resolves_local_name_without_cloud_lookup(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.data = {
            "sn-a": {"name": "Front Door", "device_id": "id-a", "serial_number": "sn-a"},
        }
        coordinator.async_set_device_parameter = AsyncMock()
        coordinator.async_request_refresh = AsyncMock()
        hass = make_hass(coordinator)
        handlers = await registered_handlers(hass)

        await handlers["set_parameter"](Mock(data={
            "device_name": "Front Door",
            "parameter_name": "MOTION_DET_ENABLE",
            "value": 0,
        }))

        coordinator.async_set_device_parameter.assert_awaited_once_with(
            "sn-a", "150", 0,
            device={"name": "Front Door", "device_id": "id-a", "serial_number": "sn-a"},
        )
        coordinator.client.find_device_by_name.assert_not_called()

    asyncio.run(run())


def test_set_parameter_falls_back_to_cloud_lookup_for_unknown_device(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.data = {"sn-a": {"name": "Front Door", "device_id": "id-a", "serial_number": "sn-a"}}
        coordinator.client.find_device_by_name.return_value = {
            "name": "Garage", "device_id": "id-g", "serial_number": "sn-g"
        }
        coordinator.client.set_device_config.return_value = True
        coordinator.async_request_refresh = AsyncMock()
        hass = make_hass(coordinator)
        handlers = await registered_handlers(hass)

        # Real write path (no mocked coordinator method): the device exists
        # in the cloud but not yet in coordinator data.
        await handlers["set_parameter"](Mock(data={
            "device_name": "Garage",
            "parameter_name": "LED_ENABLE",
            "value": 1,
        }))

        # The write still targets the exact resolved identity.
        coordinator.client.set_device_config.assert_called_once_with(
            "sn-g", {"103": 1}, device_id="id-g"
        )

    asyncio.run(run())


def test_get_device_info_returns_the_requested_identity_with_duplicate_names(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.data = {
            "sn-a": {"name": "Camera", "device_id": "id-a", "serial_number": "sn-a"},
            "sn-b": {"name": "Camera", "device_id": "id-b", "serial_number": "sn-b"},
        }
        # The library re-resolves by name and picks the first match.
        coordinator.client.get_device_info.return_value = {
            "name": "Camera", "device_id": "id-a", "serial_number": "sn-a",
        }
        coordinator.client.get_device_status.return_value = {"online": True}
        coordinator.client.get_device_config.return_value = {"iot": {"154": 75}}
        hass = make_hass(coordinator)
        handlers = await registered_handlers(hass)

        await handlers["get_device_info"](Mock(data={"device_id": "id-b"}))

        event_name, event_data = hass.bus.async_fire.call_args.args
        assert event_name == "cloudedge_device_info"
        info = event_data["device_info"]
        assert info["device_id"] == "id-b"
        assert info["serial_number"] == "sn-b"
        assert info["online"] is True
        assert info["configuration"]["154"]["value"] == 75
        # Reads went through the exact identity, not the ambiguous name.
        coordinator.client.get_device_status.assert_called_once_with("id-b")
        coordinator.client.get_device_config.assert_called_once_with("sn-b")

    asyncio.run(run())


def test_get_device_info_uses_library_result_when_identity_matches(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.data = {
            "sn-a": {"name": "Front Door", "device_id": "id-a", "serial_number": "sn-a"},
        }
        coordinator.client.get_device_info.return_value = {
            "name": "Front Door", "device_id": "id-a", "serial_number": "sn-a",
            "extra": "rich payload",
        }
        hass = make_hass(coordinator)
        handlers = await registered_handlers(hass)

        await handlers["get_device_info"](Mock(data={"device_name": "Front Door"}))

        _, event_data = hass.bus.async_fire.call_args.args
        assert event_data["device_info"]["extra"] == "rich payload"
        coordinator.client.get_device_status.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize("value", ["Garage", "00123", "", 1.5, 1])
def test_set_parameter_preserves_value_through_real_write(tmp_path, value):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.data = {
            "sn-a": {"name": "Camera", "device_id": "id-a", "serial_number": "sn-a"},
        }
        coordinator.client.set_device_config.return_value = True
        coordinator.client.get_device_config.return_value = {}
        coordinator.async_request_refresh = AsyncMock()
        handlers = await registered_handlers(make_hass(coordinator))

        await handlers["set_parameter"](Mock(data={
            "device_id": "id-a",
            "parameter_name": "DEVICE_NAME",
            "value": value,
        }))

        coordinator.client.set_device_config.assert_called_once_with(
            "sn-a", {"72": value}, device_id="id-a"
        )
        written = coordinator.client.set_device_config.call_args.args[1]["72"]
        assert type(written) is type(value)

    asyncio.run(run())


@pytest.mark.parametrize(("value", "expected"), [(True, 1), (False, 0)])
def test_set_parameter_normalizes_boolean_switch_values(tmp_path, value, expected):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.data = {
            "sn-a": {"name": "Camera", "device_id": "id-a", "serial_number": "sn-a"},
        }
        coordinator.client.set_device_config.return_value = True
        coordinator.client.get_device_config.return_value = {}
        coordinator.async_request_refresh = AsyncMock()
        handlers = await registered_handlers(make_hass(coordinator))

        await handlers["set_parameter"](Mock(data={
            "device_id": "id-a",
            "parameter_name": "LED_ENABLE",
            "value": value,
        }))

        coordinator.client.set_device_config.assert_called_once_with(
            "sn-a", {"103": expected}, device_id="id-a"
        )
        written = coordinator.client.set_device_config.call_args.args[1]["103"]
        assert type(written) is int

    asyncio.run(run())


def test_set_parameter_rejects_unknown_target_or_parameter(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.data = {"sn-a": {"name": "Front Door", "device_id": "id-a", "serial_number": "sn-a"}}
        coordinator.async_set_device_parameter = AsyncMock()
        hass = make_hass(coordinator)
        handlers = await registered_handlers(hass)

        with pytest.raises(HomeAssistantError, match="device_name or device_id"):
            await handlers["set_parameter"](Mock(data={
                "parameter_name": "LED_ENABLE",
                "value": 1,
            }))

        with pytest.raises(HomeAssistantError, match="Unknown parameter"):
            await handlers["set_parameter"](Mock(data={
                "device_name": "Front Door",
                "parameter_name": "NOT_A_REAL_PARAMETER",
                "value": 1,
            }))

        coordinator.async_set_device_parameter.assert_not_called()

    asyncio.run(run())


def test_set_parameter_unknown_device_id_never_falls_back_to_name(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.data = {"sn-a": {"name": "Front Door", "device_id": "id-a", "serial_number": "sn-a"}}
        hass = make_hass(coordinator)
        handlers = await registered_handlers(hass)

        with pytest.raises(HomeAssistantError, match="not found"):
            await handlers["set_parameter"](Mock(data={
                "device_id": "id-missing",
                "parameter_name": "LED_ENABLE",
                "value": 1,
            }))
        # A wrong device_id must not silently degrade into a name lookup.
        coordinator.client.find_device_by_name.assert_not_called()

    asyncio.run(run())


def test_refresh_parameters_targets_serial_number(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.data = {
            "sn-a": {"name": "Camera", "device_id": "id-a", "serial_number": "sn-a"},
            "sn-b": {"name": "Camera", "device_id": "id-b", "serial_number": "sn-b"},
        }
        coordinator.async_refresh_device_config = AsyncMock(return_value=True)
        hass = make_hass(coordinator)
        handlers = await registered_handlers(hass)

        await handlers["refresh_parameters"](Mock(data={"device_id": "id-b"}))

        coordinator.async_refresh_device_config.assert_awaited_once_with(
            "Camera", serial_number="sn-b"
        )

    asyncio.run(run())


def test_refresh_device_without_target_refreshes_every_coordinator(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.async_request_refresh = AsyncMock()
        hass = make_hass(coordinator)
        handlers = await registered_handlers(hass)

        await handlers["refresh_device"](Mock(data={}))

        coordinator.async_request_refresh.assert_awaited_once()

    asyncio.run(run())
