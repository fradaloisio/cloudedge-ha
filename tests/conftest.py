"""Shared test helpers for the CloudEdge test suite."""
from __future__ import annotations

try:
    from homeassistant.helpers import device_registry as dr, entity_registry as er
except ImportError:  # pragma: no cover - exercised only without HA installed
    dr = er = None


async def load_empty_registries(hass) -> None:
    """Initialize empty device/entity registries across HA generations."""
    if dr is None:
        raise RuntimeError("homeassistant is not installed")
    if hasattr(dr, "async_setup"):
        # Older Home Assistant: explicit setup + storage load.
        dr.async_setup(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
    else:
        # Home Assistant >= 2025.x removed async_setup and the load_empty
        # keyword; module-level async_load is now the entry point.
        await dr.async_load(hass)
        await er.async_load(hass)
