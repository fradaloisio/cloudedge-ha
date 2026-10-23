import importlib.util
import json
from pathlib import Path

import pytest
from cloudedge import get_available_live_stream_ids


MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "cloudedge"
    / "stream_profile_policy.py"
)
SPEC = importlib.util.spec_from_file_location("stream_profile_policy", MODULE_PATH)
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


def test_adaptive_camera_maps_auto_to_105_and_sd_to_lowest_resolution():
    device = {
        "capability_version": 81,
        "adb": 1,
        "bps2": {
            "0": "2304x1296@15",
            "2": "640x360@15",
        },
    }

    assert POLICY.resolve_stream_profile_ids(device) == (105, 102, (105, 100, 102))


def test_fixed_modern_camera_maps_profile_keys_to_100_family():
    device = {
        "capability_version": 80,
        "bps2": {
            "0": "1920x1080@15",
            "3": "320x180@10",
        },
    }

    assert POLICY.resolve_stream_profile_ids(device) == (100, 103, (100, 103))


def test_camera_without_capabilities_keeps_legacy_zero_one_fallback():
    assert POLICY.resolve_stream_profile_ids({}) == (0, 1, (0, 1))


def test_single_profile_camera_does_not_invent_an_unsupported_stream():
    device = {"bps2": {"2": "1280x720@15"}}

    assert POLICY.resolve_stream_profile_ids(device) == (102, 102, (102,))


# Shapes reported by real cameras: capability v79 advertises its small
# profile first (the Android default, 100); v91 is adaptive (105).
V79_1080 = {"type_id": 5, "capability_version": 79, "bps2": {"0": "640x360", "1": "1920x1080"}}
V79_1296 = {"type_id": 5, "capability_version": 79, "bps2": {"0": "640x360", "2": "2304x1296"}}
V91_ADAPTIVE = {"type_id": 5, "capability_version": 91, "adb": 1,
                "bps2": {"0": "640x360", "2": "2304x1296"}}


@pytest.mark.parametrize("device,expected", [
    (V79_1080, (101, 100, (100, 101))),
    (V79_1296, (102, 100, (100, 102))),
    (V91_ADAPTIVE, (105, 100, (105, 100, 102))),
    # Legacy dual-stream cameras without sizes: 0 = main, 1 = sub.
    ({"vst": 0}, (0, 1, (0, 1))),
    ({"bps": 3}, (0, 1, (0, 1))),
    # Type 16 only offers stream 0, whatever sizes bps2 advertises.
    ({"type_id": 16, "bps2": {"0": "1920x1080@15", "2": "640x360@15"}}, (0, 0, (0,))),
    ({"type_id": 16, "capability_version": 79, "bps2": {"0": "640x360", "2": "2304x1296"}},
     (0, 0, (0,))),
])
def test_hd_is_the_largest_profile_and_sd_the_smallest(device, expected):
    assert POLICY.resolve_stream_profile_ids(device) == expected


@pytest.mark.parametrize("device,expected", [
    # The default already is the lightest profile: Auto never switches.
    (V79_1296, (100, 100)),
    (V91_ADAPTIVE, (105, 100)),
    ({"capability_version": 80, "bps2": {"0": "1920x1080@15", "3": "320x180@10"}},
     (100, 103)),
    # Android starts legacy cameras on the sub stream.
    ({"vst": 0}, (1, 1)),
    ({}, (0, 1)),
])
def test_auto_starts_on_the_default_and_falls_back_only_to_a_lighter_profile(
    device, expected
):
    assert POLICY.resolve_auto_stream_ids(device) == expected


# Same fixtures as pycloudedge's test_selected_stream_always_belongs_to_available_streams.
@pytest.mark.parametrize("device", [
    {},
    {"vst": 1},
    {"vst": 2},
    {"bps": (1 << 0) | (1 << 2)},
    {"bps": 1 << 1},
    {"bps": 1 << 10},
    {"bps": (1 << 1) | (1 << 10)},
    {"type_id": 16, "capability_version": 81, "adb": 1, "bps2": {"2": "640x360@15"}},
    {"type_id": 16, "bps2": {"0": "1920x1080@15", "2": "640x360@15"}},
    {"bps2": {"2": "1280x720@15"}},
    {"bps2": {"4": "640x360@15"}},
    {"bps2": {"0": "2304x1296@15", "4": "512x288@10"}},
    {"capability_version": 81, "adb": 1, "bps2": {"0": "2304x1296@15", "2": "640x360@15"}},
    {"msc": [{"v_id": 1, "bps2": {"3": "320x180@10"}}]},
    {"msc": json.dumps([{"v_id": 2, "bps2": {"4": "640x360@15"}}])},
    {"bps2": json.dumps({"4": "640x360@15"})},
])
def test_hd_sd_and_auto_only_open_streams_the_library_offers(device):
    hd_id, sd_id, _available = POLICY.resolve_stream_profile_ids(device)
    auto_id, auto_fallback_id = POLICY.resolve_auto_stream_ids(device)

    offered = set(get_available_live_stream_ids(device))
    assert {hd_id, sd_id, auto_id, auto_fallback_id} <= offered
