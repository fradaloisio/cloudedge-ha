"""Changing/restoring a profile must leave HA's source-restart loop runnable."""
import asyncio
import threading
from unittest.mock import AsyncMock, Mock, patch

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from custom_components.cloudedge import CloudEdgeCoordinator
from custom_components.cloudedge.select import CloudEdgeStreamProfileSelect


@pytest.mark.parametrize("restore", [False, True], ids=["select", "restore"])
def test_profile_change_does_not_block_source_restart_on_ha_loop(tmp_path, restore):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        with patch("homeassistant.helpers.frame.report_usage"):
            coordinator = CloudEdgeCoordinator(
                hass, "user@example.test", "password", "IT", "+39", 5,
                Mock(entry_id="profile-test"),
            )
        selected = []

        def change_profile(serial, profile):
            # This is the format-switch lock's real contract: the blocking
            # worker must allow a callback on HA's loop to finish the restart.
            restarted = threading.Event()
            hass.loop.call_soon_threadsafe(restarted.set)
            assert restarted.wait(1), "Profile setter blocked the HA restart loop"
            selected.append((serial, profile))

        coordinator.set_stream_profile = change_profile
        entity = CloudEdgeStreamProfileSelect(coordinator, "camera", {"name": "Camera"})
        entity.hass = hass
        entity.entity_id = "select.camera_stream_profile"
        try:
            if restore:
                with patch.object(CoordinatorEntity, "async_added_to_hass", AsyncMock()):
                    entity.async_get_last_state = AsyncMock(return_value=State(entity.entity_id, "SD"))
                    await entity.async_added_to_hass()
            else:
                # This focused entity test does not load an HA entity platform.
                with patch.object(entity, "async_write_ha_state"):
                    await entity.async_select_option("SD")
            assert selected == [("camera", "SD")]
        finally:
            await coordinator.async_shutdown()
            await asyncio.sleep(0)

    asyncio.run(run())
