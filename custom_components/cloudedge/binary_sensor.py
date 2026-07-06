"""Binary sensor platform for CloudEdge integration.

Provides a motion-detection binary sensor per camera, driven by MQTT push
events from the CloudEdge/Meari broker.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import CloudEdgeCoordinator
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Seconds after the last MQTT event before the sensor reverts to "Clear"
MOTION_CLEAR_DELAY = 60


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up CloudEdge binary sensor platform."""
    coordinator: CloudEdgeCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    if not coordinator.data:
        _LOGGER.warning("No device data available yet for binary sensors — will retry on first update")

        @callback
        def _async_add_when_ready() -> None:
            if coordinator.data and not getattr(coordinator, "_binary_sensors_added", False):
                coordinator._binary_sensors_added = True
                entities: list[BinarySensorEntity] = []
                for sn, info in coordinator.data.items():
                    entities.append(CloudEdgeMotionSensor(coordinator, sn, info))
                _LOGGER.info("Adding %d motion binary-sensor entities (deferred)", len(entities))
                async_add_entities(entities)

        coordinator.async_add_listener(_async_add_when_ready)
        return

    entities: list[BinarySensorEntity] = []
    for serial_number, device_info in coordinator.data.items():
        entities.append(
            CloudEdgeMotionSensor(coordinator, serial_number, device_info)
        )

    _LOGGER.info("Adding %d motion binary-sensor entities", len(entities))
    async_add_entities(entities)


class CloudEdgeMotionSensor(
    CoordinatorEntity[CloudEdgeCoordinator], BinarySensorEntity
):
    """Binary sensor that reflects real-time MQTT motion events."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.MOTION
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coordinator: CloudEdgeCoordinator,
        serial_number: str,
        device_info: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._serial_number = serial_number
        self._device_info = device_info
        self._attr_unique_id = f"{DOMAIN}_{serial_number}_motion"
        self._attr_name = "Motion"
        self._clear_unsub: Any = None

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._serial_number)},
            "name": self._device_info.get("name", f"Camera {self._serial_number}"),
            "manufacturer": "CloudEdge",
            "model": self._device_info.get("type", "SmartEye Camera"),
            "serial_number": self._serial_number,
            "sw_version": self._device_info.get("firmware_version"),
        }

    @property
    def is_on(self) -> bool:
        """Return True while a recent motion event is active."""
        device_data = (self.coordinator.data or {}).get(self._serial_number, {})
        last_time = device_data.get("last_motion_time")
        if not last_time:
            return False
        return (time.time() - last_time) < MOTION_CLEAR_DELAY

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        device_data = (self.coordinator.data or {}).get(self._serial_number, {})
        attrs: dict[str, Any] = {}
        last_event = device_data.get("last_motion_event")
        if last_event:
            attrs["event_type"] = last_event
        last_time = device_data.get("last_motion_time")
        if last_time:
            from datetime import datetime, timezone

            attrs["last_triggered"] = datetime.fromtimestamp(
                last_time, tz=timezone.utc
            ).isoformat()
        return attrs

    @callback
    def _handle_coordinator_update(self) -> None:
        """React to coordinator data push (including MQTT events)."""
        super()._handle_coordinator_update()

        # Always reschedule while on: a new motion event during a pending
        # timer must push the clear moment forward, otherwise the timer
        # fires while is_on is still true and the sensor sticks "on".
        if self.is_on:
            self._schedule_clear()

    def _schedule_clear(self) -> None:
        """(Re)schedule turning the sensor off when the motion window ends."""
        if self._clear_unsub is not None:
            self._clear_unsub()
            self._clear_unsub = None

        device_data = (self.coordinator.data or {}).get(self._serial_number, {})
        last_time = device_data.get("last_motion_time")
        if not last_time:
            return
        # Fire just after the motion window closes (0.5s margin so is_on
        # has definitely flipped to False when the timer runs).
        delay = max(0.0, last_time + MOTION_CLEAR_DELAY - time.time()) + 0.5

        @callback
        def _clear(_now: Any) -> None:
            self._clear_unsub = None
            if self.is_on:
                # New motion arrived while the timer was pending
                self._schedule_clear()
            self.async_write_ha_state()

        self._clear_unsub = async_call_later(self.hass, delay, _clear)

    async def async_will_remove_from_hass(self) -> None:
        """Cancel pending timer on removal."""
        if self._clear_unsub is not None:
            self._clear_unsub()
            self._clear_unsub = None
        await super().async_will_remove_from_hass()
