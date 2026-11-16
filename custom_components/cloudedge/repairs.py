"""Repairs for the CloudEdge integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.camera.const import DATA_CAMERA_PREFS, DATA_COMPONENT
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant


class PreloadRepairFlow(RepairsFlow):
    """Turn "Preload camera stream" off for a battery camera."""

    def __init__(self, entity_id: str, camera: str) -> None:
        self._entity_id = entity_id
        self._camera = camera

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Ask before changing the camera's preference."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Turn the preload off once the user confirms."""
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders={"camera": self._camera},
            )
        await self.hass.data[DATA_CAMERA_PREFS].async_update(
            self._entity_id, preload_stream=False
        )
        # Without the preload, Home Assistant removes a stream output when its
        # idle timer fires, and ends the stream with the last output. A
        # preloaded output's timer may already have fired, or never have
        # started: it starts with the first video or the first viewer.
        camera = self.hass.data[DATA_COMPONENT].get_entity(self._entity_id)
        stream = getattr(camera, "stream", None)
        if stream is not None:
            for output in list(stream.outputs().values()):
                if output.idle:
                    await stream.remove_provider(output)
                else:
                    output.idle_timer.start()
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Create the fix flow of a CloudEdge repair issue."""
    return PreloadRepairFlow(data["entity_id"], data["camera"])
