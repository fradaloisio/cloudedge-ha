"""Config entry options: polling and live-stream settings."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_BATTERY_KEEPALIVE,
    CONF_BATTERY_MAX_LIVE_MINUTES,
    CONF_REFRESH_INTERVAL,
    CONF_STREAM_AUDIO,
    DEFAULT_BATTERY_KEEPALIVE,
    DEFAULT_BATTERY_MAX_LIVE_MINUTES,
    DEFAULT_REFRESH_INTERVAL,
    DEFAULT_STREAM_AUDIO,
)
from .stream_bridge import StreamOptions


def entry_options(entry: ConfigEntry) -> dict[str, Any]:
    """Return every option of the entry, with its default when never set."""
    options = entry.options
    return {
        # Before the options existed, the interval was chosen at setup.
        CONF_REFRESH_INTERVAL: options.get(
            CONF_REFRESH_INTERVAL,
            entry.data.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL),
        ),
        CONF_BATTERY_MAX_LIVE_MINUTES: options.get(
            CONF_BATTERY_MAX_LIVE_MINUTES, DEFAULT_BATTERY_MAX_LIVE_MINUTES
        ),
        CONF_BATTERY_KEEPALIVE: options.get(
            CONF_BATTERY_KEEPALIVE, DEFAULT_BATTERY_KEEPALIVE
        ),
        CONF_STREAM_AUDIO: options.get(CONF_STREAM_AUDIO, DEFAULT_STREAM_AUDIO),
    }


def stream_options(entry: ConfigEntry) -> StreamOptions:
    """Return the entry's live-stream settings."""
    options = entry_options(entry)
    return StreamOptions(
        audio=options[CONF_STREAM_AUDIO],
        battery_keepalive=options[CONF_BATTERY_KEEPALIVE],
        battery_max_live=60.0 * options[CONF_BATTERY_MAX_LIVE_MINUTES],
    )
