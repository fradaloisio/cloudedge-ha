"""Shared entity helpers: camera filtering and device metadata."""
import pytest

pytest.importorskip("homeassistant")

from custom_components.cloudedge.entity import (
    CAMERA_TYPE_IDS,
    CloudEdgeEntityMixin,
    is_camera_device,
)


@pytest.mark.parametrize("type_id", CAMERA_TYPE_IDS)
def test_known_camera_type_ids_are_camera_devices(type_id):
    assert is_camera_device({"type_id": type_id})


def test_non_camera_and_missing_type_ids_are_not_camera_devices():
    assert not is_camera_device({"type_id": 99})
    assert not is_camera_device({})
    assert not is_camera_device({"name": "Chime"})
    assert not is_camera_device(None)


def test_mixin_builds_device_registry_metadata():
    class Entity(CloudEdgeEntityMixin):
        _serial_number = "sn-1"
        _device_info = {"name": "Garden", "type": "SmartEye C9", "firmware_version": "1.2.3"}

    info = Entity().device_info
    assert info["identifiers"] == {("cloudedge", "sn-1")}
    assert info["name"] == "Garden"
    assert info["model"] == "SmartEye C9"
    assert info["serial_number"] == "sn-1"
    assert info["sw_version"] == "1.2.3"
    assert info["manufacturer"] == "CloudEdge"


def test_mixin_falls_back_when_device_fields_are_missing():
    class Entity(CloudEdgeEntityMixin):
        _serial_number = "sn-2"
        _device_info = {}

    info = Entity().device_info
    assert info["name"] == "Camera sn-2"
    assert info["model"] == "SmartEye Camera"
    assert info["sw_version"] is None
