"""Map Home Assistant quality choices to camera-specific stream IDs."""

from __future__ import annotations

from typing import Any

from cloudedge import (
    get_available_live_stream_ids,
    select_default_live_stream_id,
    select_live_stream_id,
)


def resolve_stream_profile_ids(
    device: dict[str, Any],
) -> tuple[int, int, tuple[int, ...]]:
    """Return main, low-bandwidth and available stream IDs for a camera."""
    available = list(dict.fromkeys(get_available_live_stream_ids(device)))
    main_id = select_default_live_stream_id(device)
    low_id = select_live_stream_id(device, prefer_low=True)

    if main_id not in available:
        available.insert(0, main_id)
    if low_id not in available:
        available.append(low_id)

    if low_id == main_id and len(available) > 1:
        low_id = next(
            candidate for candidate in reversed(available) if candidate != main_id
        )

    return main_id, low_id, tuple(available)
