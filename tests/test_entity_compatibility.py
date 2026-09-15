"""Regression coverage for existing dashboards and late device parameters."""
import asyncio

import pytest

pytest.importorskip("homeassistant")

from homeassistant.helpers import entity_registry as er, device_registry as dr
from custom_components.cloudedge import sensor, switch
from custom_components.cloudedge.const import DOMAIN, SWITCH_PARAMETERS, SENSOR_PARAMETERS
from cloudedge.iot_parameters import IOT_PARAMETERS, BOOLEAN_PARAMETERS
from test_coordinator import make_coordinator


def device(config):
    return {"name": "Camera", "device_id": "id", "serial_number": "sn", "configuration": config}


@pytest.mark.parametrize("code,curated", [(code, name) for name, code in SWITCH_PARAMETERS.items()])
@pytest.mark.parametrize("has_curated", [False, True])
def test_upgrade_preserves_registered_legacy_switches(tmp_path, code, curated, has_curated):
    async def run():
        coordinator = make_coordinator(tmp_path)
        hass = coordinator.hass
        dr.async_setup(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        registry = er.async_get(hass)
        legacy_id = f"cloudedge_sn_{IOT_PARAMETERS[code]['name'].lower()}_switch"
        old = registry.async_get_or_create(
            "switch", DOMAIN, legacy_id, suggested_object_id="my_dashboard_switch",
        )
        registry.async_update_entity(old.entity_id, name="My custom name")
        curated_id = f"cloudedge_sn_{curated}"
        if has_curated:
            registry.async_get_or_create("switch", DOMAIN, curated_id)
        coordinator.data = {"sn": device({code: {"value": "1"}})}
        hass.data[DOMAIN] = {coordinator.config_entry.entry_id: coordinator}
        entities = []
        await switch.async_setup_entry(hass, coordinator.config_entry, entities.extend)
        assert {e.unique_id for e in entities} == (
            {legacy_id, curated_id} if has_curated else {legacy_id}
        )
        assert registry.async_get_entity_id("switch", DOMAIN, legacy_id) == old.entity_id
        assert registry.async_get(old.entity_id).name == "My custom name"
        assert all(e.is_on for e in entities)
        coordinator.async_update_listeners()
        assert len(entities) == (2 if has_curated else 1)
    asyncio.run(run())


@pytest.mark.parametrize("platform", [sensor, switch])
@pytest.mark.parametrize("empty_inventory", [False, True])
def test_parameters_arriving_after_startup_add_entities_without_reload(tmp_path, platform, empty_inventory):
    async def run():
        coordinator = make_coordinator(tmp_path)
        hass = coordinator.hass
        dr.async_setup(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        hass.data[DOMAIN] = {coordinator.config_entry.entry_id: coordinator}
        coordinator.data = None if empty_inventory else {"sn": device({})}
        entities = []
        await platform.async_setup_entry(hass, coordinator.config_entry, entities.extend)
        initial = {e.unique_id for e in entities}
        coordinator.data = {"sn": device({"154": {"value": 75}, "150": {"value": "1"}})}
        coordinator.async_update_listeners()
        expected = "cloudedge_sn_battery_level" if platform is sensor else "cloudedge_sn_motion_detection"
        assert expected not in initial
        assert expected in {e.unique_id for e in entities}
        count = len(entities)
        coordinator.async_update_listeners()
        assert len(entities) == count
        coordinator.config_entry.async_on_unload.assert_called()
        unsubscribe = coordinator.config_entry.async_on_unload.call_args.args[0]
        unsubscribe()
        coordinator.data["second"] = device({"154": {"value": 50}, "150": {"value": "0"}})
        coordinator.async_update_listeners()
        assert len(entities) == count
    asyncio.run(run())


def test_all_sensor_identities_match_the_legacy_schema(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        config = {code: {"value": 1} for code in IOT_PARAMETERS}
        coordinator.data = {"sn": device(config)}
        hass = coordinator.hass
        dr.async_setup(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        hass.data[DOMAIN] = {coordinator.config_entry.entry_id: coordinator}
        entities = []
        await sensor.async_setup_entry(hass, coordinator.config_entry, entities.extend)
        expected = {"cloudedge_sn_connection_status"}
        expected.update(f"cloudedge_sn_{name}" for name, code in SENSOR_PARAMETERS.items() if code in config)
        expected.update(
            f"cloudedge_sn_{info['name'].lower()}"
            for code, info in IOT_PARAMETERS.items() if code not in SENSOR_PARAMETERS.values()
        )
        assert {e.unique_id for e in entities} == expected
        battery = next(e for e in entities if e.unique_id == "cloudedge_sn_battery_level")
        assert battery.name == "Battery Level"
        assert battery.native_value == 1
    asyncio.run(run())


def test_existing_generic_switch_inventory_is_not_lost(tmp_path):
    async def run():
        coordinator = make_coordinator(tmp_path)
        hass = coordinator.hass
        dr.async_setup(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        registry = er.async_get(hass)
        expected = set()
        for code, info in IOT_PARAMETERS.items():
            if info["name"] in BOOLEAN_PARAMETERS:
                unique_id = f"cloudedge_sn_{info['name'].lower()}_switch"
                registry.async_get_or_create("switch", DOMAIN, unique_id)
                expected.add(unique_id)
        coordinator.data = {"sn": device({code: {"value": "1"} for code in IOT_PARAMETERS})}
        hass.data[DOMAIN] = {coordinator.config_entry.entry_id: coordinator}
        entities = []
        await switch.async_setup_entry(hass, coordinator.config_entry, entities.extend)
        assert {e.unique_id for e in entities} == expected
    asyncio.run(run())


def test_status_failure_does_not_discard_battery_configuration(tmp_path):
    async def run():
        c = make_coordinator(tmp_path)
        c.client.get_all_devices.return_value = [device({})]
        c.client.get_device_status.side_effect = RuntimeError("status temporarily unavailable")
        c.client.get_device_config.return_value = {"iot": {"154": 75, "150": 1}}
        c.client.get_device_online_status.return_value = "dormancy"
        result = c._fetch_data()
        assert result["sn"]["configuration"]["154"]["value"] == 75
        assert result["sn"]["configuration"]["150"]["value"] == 1
    asyncio.run(run())


def test_temporary_config_failure_keeps_last_known_parameters(tmp_path):
    async def run():
        c = make_coordinator(tmp_path)
        config = {"154": {"value": 75}, "150": {"value": 1}}
        c.data = {"sn": device(config)}
        c.client.get_all_devices.return_value = [device({})]
        c.client.get_device_status.return_value = {"online": False}
        c.client.get_device_config.side_effect = RuntimeError("temporary timeout")
        c.client.get_device_online_status.return_value = "dormancy"
        result = c._fetch_data()
        assert result["sn"]["configuration"] == config
        assert result["sn"]["configuration"] is not config
    asyncio.run(run())


@pytest.mark.parametrize('platform,unique_id', [
    (sensor, 'cloudedge_sn_battery_level'),
    (switch, 'cloudedge_sn_motion_det_enable_switch'),
])
def test_registered_entities_load_during_cloud_outage_and_recover(tmp_path, platform, unique_id):
    async def run():
        coordinator = make_coordinator(tmp_path)
        hass = coordinator.hass
        dr.async_setup(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        domain = 'sensor' if platform is sensor else 'switch'
        registry = er.async_get(hass)
        old = registry.async_get_or_create(domain, DOMAIN, unique_id)
        coordinator.data = {'sn': device({})}
        hass.data[DOMAIN] = {coordinator.config_entry.entry_id: coordinator}
        entities = []
        await platform.async_setup_entry(hass, coordinator.config_entry, entities.extend)
        entity = next(e for e in entities if e.unique_id == unique_id)
        assert not entity.available
        assert not coordinator.data['sn']['configuration']
        coordinator.data['sn']['configuration'] = {'154': {'value': 75}, '150': {'value': 1}}
        coordinator.async_update_listeners()
        assert entity.available
        assert sum(e.unique_id == unique_id for e in entities) == 1
        assert registry.async_get_entity_id(domain, DOMAIN, unique_id) == old.entity_id
        if domain == 'sensor':
            assert entity.native_value == 75
        else:
            assert entity.is_on
    asyncio.run(run())
