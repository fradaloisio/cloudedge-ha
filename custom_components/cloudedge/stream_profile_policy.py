"""Map Home Assistant quality choices to camera-specific stream IDs."""

from __future__ import annotations

from typing import Any

from cloudedge import (
    get_available_live_stream_ids,
    get_live_stream_profiles,
    select_default_live_stream_id,
    select_live_stream_id,
    supports_adaptive_live_stream,
)

# Legacy dual-stream cameras: 0 is the main stream, 1 the sub stream.
_LEGACY_MAIN_ID = 0
_LEGACY_SUB_ID = 1


def _profile_areas(device: dict[str, Any]) -> dict[int, int]:
    """Return pixel areas of the fixed (non-adaptive) advertised profiles."""
    # Only streams the library offers for this device (type 16: stream 0 only).
    available = set(get_available_live_stream_ids(device))
    return {
        profile["video_id"]: profile["width"] * profile["height"]
        for profile in get_live_stream_profiles(device)
        if not profile.get("adaptive") and profile.get("width") and profile.get("height")
        and profile["video_id"] in available
    }


def _available_ids(device: dict[str, Any], *extra: int) -> list[int]:
    available = list(dict.fromkeys(get_available_live_stream_ids(device)))
    for stream_id in extra:
        if stream_id not in available:
            available.append(stream_id)
    return available


def resolve_stream_profile_ids(
    device: dict[str, Any],
) -> tuple[int, int, tuple[int, ...]]:
    """Return HD, SD and available stream IDs for a camera.

    The Android default (first bps2 key) is NOT necessarily the largest
    profile: several battery cameras advertise ``{"0": "640x360",
    "2": "2304x1296"}``. HD is therefore the adaptive stream when supported,
    else the largest advertised profile; SD is the smallest. Without size
    information, legacy 0/1 cameras keep 0 = main and 1 = sub.
    """
    default_id = select_default_live_stream_id(device)
    sd_id = select_live_stream_id(device, prefer_low=True)
    available = _available_ids(device)
    if default_id not in available:
        available.insert(0, default_id)
    if sd_id not in available:
        available.append(sd_id)

    areas = _profile_areas(device)
    if supports_adaptive_live_stream(device):
        hd_id = default_id
    elif areas:
        hd_id = max(areas, key=areas.__getitem__)
    elif _LEGACY_MAIN_ID in available and _LEGACY_SUB_ID in available:
        hd_id, sd_id = _LEGACY_MAIN_ID, _LEGACY_SUB_ID
    else:
        hd_id = default_id

    # SD cannot exceed HD: HD is adaptive or the largest sized profile, SD the smallest.
    return hd_id, sd_id, tuple(available)


def resolve_auto_stream_ids(device: dict[str, Any]) -> tuple[int, int]:
    """Return Auto's start ID (Android default) and its degraded fallback.

    The fallback is only ever a strictly lighter profile; when the default
    already is the lightest one, the fallback equals the start ID (no switch).
    """
    auto_id = select_default_live_stream_id(device)
    _hd_id, sd_id, _available = resolve_stream_profile_ids(device)
    if sd_id == auto_id:
        return auto_id, auto_id

    areas = _profile_areas(device)
    if supports_adaptive_live_stream(device) and auto_id == 105:
        lighter = True
    elif auto_id in areas and sd_id in areas:
        lighter = areas[sd_id] < areas[auto_id]
    else:
        # No size information: only the legacy main -> sub step is known lighter.
        lighter = (auto_id, sd_id) == (_LEGACY_MAIN_ID, _LEGACY_SUB_ID)
    return auto_id, (sd_id if lighter else auto_id)
