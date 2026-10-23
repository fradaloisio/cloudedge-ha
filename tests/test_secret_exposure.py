"""The camera's P2P credentials never reach states, events or logs."""
import asyncio
import json
import logging
from unittest.mock import Mock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.helpers import entity_registry as er
from cloudedge.iot_parameters import IOT_PARAMETERS
from custom_components.cloudedge import sensor
from custom_components.cloudedge.const import DOMAIN
from custom_components.cloudedge.sensor import CloudEdgeDeviceStatusSensor

from conftest import load_empty_registries
from test_coordinator import make_coordinator
from test_services import make_hass, registered_handlers

SECRETS = {
    "host_key": "HOSTKEY-FAKE",
    "host_key1": "HOSTKEY1-FAKE",
    "share_access_sign": "SIGN-FAKE",
    "p2p_init": "P2PINIT-FAKE",
    "device_p2p": "DEVP2P-FAKE",
    "relay_license_id": "LICENCE-FAKE",
    "device_uuid": "UUID-FAKE",
}

# IoT parameters holding device secrets: DEVICE_KEY, SAFE_PASSWORD, VIDEO_PASSWORD.
SECRET_PARAMETERS = {"3": "DEVKEY-FAKE", "21": "SAFEPWD-FAKE", "217": "VIDEOPWD-FAKE"}


def test_status_sensor_attributes_do_not_expose_host_key(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.data = {
            "sn-a": {"name": "Chime", "device_id": "id-a", "serial_number": "sn-a",
                     "type": "Chime", "online": True, **SECRETS},
        }
        sensor = CloudEdgeDeviceStatusSensor(
            coordinator, "sn-a", coordinator.data["sn-a"]
        )

        attributes = sensor.extra_state_attributes

        assert attributes["serial_number"] == "sn-a"
        assert not set(attributes) & set(SECRETS)
        assert not any(secret in json.dumps(attributes) for secret in SECRETS.values())

    asyncio.run(run())


def test_secret_iot_parameters_never_become_sensors(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        hass = coordinator.hass
        await load_empty_registries(hass)
        registry = er.async_get(hass)
        secret_ids = {
            f"cloudedge_{serial}_{IOT_PARAMETERS[code]['name'].lower()}"
            for serial in ("sn-a", "sn-b")
            for code in SECRET_PARAMETERS
        }
        # Earlier versions created these sensors; a user may have enabled them.
        for unique_id in secret_ids:
            registry.async_get_or_create("sensor", DOMAIN, unique_id)
        coordinator.data = {
            "sn-a": {"name": "Front Door", "device_id": "id-a", "serial_number": "sn-a",
                     "configuration": {
                         "154": {"value": 75},
                         **{code: {"value": value} for code, value in SECRET_PARAMETERS.items()},
                     }},
            # Configuration not read at startup: only registered sensors are restored.
            "sn-b": {"name": "Garage", "device_id": "id-b", "serial_number": "sn-b",
                     "configuration": {}},
        }
        hass.data[DOMAIN] = {coordinator.config_entry.entry_id: coordinator}
        entities = []

        await sensor.async_setup_entry(hass, coordinator.config_entry, entities.extend)

        unique_ids = {entity.unique_id for entity in entities}
        assert "cloudedge_sn-a_battery_level" in unique_ids
        assert not unique_ids & secret_ids
        # A registered secret sensor is not a known parameter: sn-b still gets
        # the Status sensor of a device without parameters.
        assert "cloudedge_sn-b_status" in unique_ids
        states = json.dumps(
            [(entity.native_value, entity.extra_state_attributes) for entity in entities],
            default=str,
        )
        assert not any(secret in states for secret in SECRET_PARAMETERS.values())

    asyncio.run(run())


def test_get_device_info_redacts_p2p_credentials_in_event_and_log(tmp_path, caplog):
    async def run():
        coordinator = make_coordinator(tmp_path)
        coordinator.data = {
            "sn-a": {"name": "Front Door", "device_id": "id-a", "serial_number": "sn-a"},
        }
        coordinator.client.get_device_info.return_value = {
            "name": "Front Door", "device_id": "id-a", "serial_number": "sn-a",
            "online": True, **SECRETS,
            # Stream profiles share the numeric keys of the secret IoT codes.
            "bps2": {"0": "640x360", "3": "1920x1080"},
            "configuration": {
                "154": {"name": "BATTERY_PERCENT", "code": "154", "value": 75},
                **{
                    code: {"name": IOT_PARAMETERS[code]["name"], "code": code, "value": value}
                    for code, value in SECRET_PARAMETERS.items()
                },
            },
        }
        hass = make_hass(coordinator)
        handlers = await registered_handlers(hass)

        with caplog.at_level(logging.DEBUG, logger="custom_components.cloudedge"):
            await handlers["get_device_info"](Mock(data={"device_name": "Front Door"}))

        event_name, event_data = hass.bus.async_fire.call_args.args
        assert event_name == "cloudedge_device_info"
        info = event_data["device_info"]
        # Useful, non-secret fields survive.
        assert info["serial_number"] == "sn-a"
        assert info["online"] is True
        assert info["configuration"]["154"]["value"] == 75
        assert info["bps2"] == {"0": "640x360", "3": "1920x1080"}
        payload = json.dumps(event_data)
        for secret in (*SECRETS.values(), *SECRET_PARAMETERS.values()):
            assert secret not in payload
            assert secret not in caplog.text
        # The dump is debug-only, not part of the default INFO stream.
        assert not [r for r in caplog.records
                    if r.levelno >= logging.INFO and "Device info" in r.getMessage()]

    asyncio.run(run())
