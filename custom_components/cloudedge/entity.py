"""Shared helpers for CloudEdge entity platforms."""
from __future__ import annotations

from typing import Any

from .const import DOMAIN

# Device type_ids that expose camera/live-stream capabilities. Devices
# without a matching type_id (chimes, sensors, ...) must never become
# camera entities.
CAMERA_TYPE_IDS = (1, 2, 3, 4, 5)


def is_camera_device(device_info: dict[str, Any] | None) -> bool:
    """Return True for devices that support camera/live-stream entities."""
    return bool(device_info) and device_info.get("type_id") in CAMERA_TYPE_IDS


class CloudEdgeEntityMixin:
    """Device-registry metadata shared by every CloudEdge entity."""

    _serial_number: str
    _device_info: dict[str, Any]

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self._serial_number)},
            "name": self._device_info.get("name", f"Camera {self._serial_number}"),
            "manufacturer": "CloudEdge",
            "model": self._device_info.get("type", "SmartEye Camera"),
            "serial_number": self._serial_number,
            "sw_version": self._device_info.get("firmware_version"),
        }
