import importlib.util
from pathlib import Path


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
