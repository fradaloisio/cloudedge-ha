"""Curated sensors, added the way Home Assistant adds them."""
import asyncio
import logging
from datetime import timedelta

import pytest

pytest.importorskip("homeassistant")

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform

from custom_components.cloudedge.const import DOMAIN, SENSOR_PARAMETERS
from custom_components.cloudedge.sensor import CloudEdgeConfigSensor

from conftest import load_empty_registries
from test_coordinator import make_coordinator


def test_every_curated_sensor_gets_a_state_once_enabled(tmp_path, monkeypatch):
    # The platform below has no config entry, and Home Assistant refuses a
    # device link without one.
    monkeypatch.setattr(CloudEdgeConfigSensor, "device_info", None)

    async def run():
        coordinator = make_coordinator(tmp_path)
        hass = coordinator.hass
        await load_empty_registries(hass)
        registry = er.async_get(hass)
        coordinator.last_update_success = True
        coordinator.data = {
            "sn": {
                "name": "Camera", "device_id": "id", "serial_number": "sn",
                "configuration": {
                    code: {"value": 50} for code in SENSOR_PARAMETERS.values()
                },
            },
        }
        sensors = [
            CloudEdgeConfigSensor(coordinator, "sn", coordinator.data["sn"], name, code)
            for name, code in SENSOR_PARAMETERS.items()
        ]
        # Most are disabled by default: register each one as Home Assistant
        # holds it once a user has enabled it.
        for sensor in sensors:
            registry.async_get_or_create("sensor", DOMAIN, sensor.unique_id)
        platform = EntityPlatform(
            hass=hass,
            logger=logging.getLogger(__name__),
            domain="sensor",
            platform_name=DOMAIN,
            platform=None,
            scan_interval=timedelta(seconds=30),
            entity_namespace=None,
        )

        await platform.async_add_entities(sensors)
        await hass.async_block_till_done()

        states = {
            name: hass.states.get(sensor.entity_id)
            for name, sensor in zip(SENSOR_PARAMETERS, sensors)
        }
        await hass.async_stop(force=True)
        without_value = {
            name for name, state in states.items()
            if state is None or float(state.state) != 50
        }
        assert without_value == set()

    asyncio.run(run())
