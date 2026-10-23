"""NAL parsing: the single-pass parser against the byte-by-byte scan it replaced.

on_video scanned every access unit about five times, byte by byte in Python.
The parser now finds start codes with bytes.find and splits each unit once.
Nothing a camera or a broken transport can deliver may change its results, so
these tests compare it with a verbatim copy of the previous helpers on
randomized H.264 and HEVC access units.
"""
from __future__ import annotations

import random
from unittest.mock import Mock

import pytest

pytest.importorskip("homeassistant")

from custom_components.cloudedge import stream_bridge as module
from test_stream_recovery import make_bridge


# Reference: the helpers before the single-pass parser, copied verbatim.

def _convert_to_annex_b(frame_data: bytes) -> bytes:
    """Convert a single H.264/HEVC access unit to Annex B if needed."""
    if not frame_data:
        return frame_data

    if frame_data.startswith(b"\x00\x00\x00\x01") or frame_data.startswith(b"\x00\x00\x01"):
        return frame_data

    converted = bytearray()
    offset = 0
    remaining = len(frame_data)

    while remaining >= 4:
        nal_len = int.from_bytes(frame_data[offset:offset + 4], "big")
        next_offset = offset + 4 + nal_len
        if nal_len <= 0 or next_offset > len(frame_data):
            break
        converted.extend(b"\x00\x00\x00\x01")
        converted.extend(frame_data[offset + 4:next_offset])
        offset = next_offset
        remaining = len(frame_data) - offset

    if converted and offset == len(frame_data):
        return bytes(converted)

    return b"\x00\x00\x00\x01" + frame_data


def _annex_b_nal_headers(frame_data: bytes):
    """Yield the first byte of each Annex B NAL unit."""
    for nal in _iter_nal_units(frame_data):
        yield nal[0]


def _detect_video_codec(frame_data: bytes) -> str | None:
    """Detect H.264 or HEVC from parameter-set NAL units."""
    # H.264 P slices commonly begin with 0x41, which superficially looks like
    # HEVC type 32. HEVC headers have a second byte, layer zero and a nonzero
    # temporal id; validate those bits before interpreting parameter sets.
    hevc_types = {
        (nal[0] >> 1) & 0x3F
        for nal in _iter_nal_units(frame_data)
        if len(nal) >= 2 and not nal[0] & 0x81
        and nal[1] >> 3 == 0 and nal[1] & 7
    }
    if hevc_types.intersection({32, 33, 34}):
        return "hevc"
    # H.264 only from its SPS. HEVC STSA_N, IDR_N_LP and EOS headers (0x08,
    # 0x28, 0x48) share the H.264 PPS type bits; a base-layer HEVC header is
    # even, so it can never read as an SPS (7).
    if any((header & 0x1F) == 7 for header in _annex_b_nal_headers(frame_data)):
        return "h264"
    return None


def _is_video_keyframe(frame_data: bytes, codec: str) -> bool:
    """Return True if the access unit contains an actual keyframe slice.

    Parameter sets (SPS/PPS/VPS) alone do NOT count: an IDR-less access
    unit cannot be decoded, and an IDR without parameter sets is handled
    separately by prepending cached ones.
    """
    headers = list(_annex_b_nal_headers(frame_data))
    if codec == "h264":
        return any((header & 0x1F) == 5 for header in headers)
    return any(((header >> 1) & 0x3F) in {19, 20} for header in headers)


def _iter_nal_units(frame_data: bytes):
    """Yield each Annex B NAL unit payload (start code stripped)."""
    positions = []
    i = 0
    n = len(frame_data)
    while i + 3 <= n:
        if frame_data[i:i + 3] == b"\x00\x00\x01":
            positions.append(i + 3)
            i += 3
        else:
            i += 1
    for idx, start in enumerate(positions):
        end = positions[idx + 1] - 3 if idx + 1 < len(positions) else n
        # Drop the leading zero that belongs to the next 4-byte start code
        while end > start and idx + 1 < len(positions) and frame_data[end - 1] == 0:
            end -= 1
        payload = frame_data[start:end]
        if payload:
            yield payload


def _extract_parameter_sets(frame_data: bytes, codec: str) -> bytes:
    """Return the SPS/PPS (H.264) or VPS/SPS/PPS (HEVC) NAL units."""
    out = bytearray()
    for nal in _iter_nal_units(frame_data):
        if codec == "h264":
            is_ps = (nal[0] & 0x1F) in (7, 8)
        else:
            is_ps = ((nal[0] >> 1) & 0x3F) in (32, 33, 34)
        if is_ps:
            out += b"\x00\x00\x00\x01" + nal
    return bytes(out)


def reference_on_video(frames):
    """Replay one attempt through on_video's decisions, from the reference.

    Return what on_video hands on: (access unit, keyframe) for every enqueued
    frame and (codec, parameter sets, first keyframe) for every muxer start.
    An intended change to on_video's gating belongs here as well.
    """
    delivered, starts = [], []
    attempt_codec = None
    parameter_cache: dict[int, bytes] = {}
    got_keyframe = False
    for frame_data in frames:
        frame_data = _convert_to_annex_b(frame_data)
        detected_codec = _detect_video_codec(frame_data)
        if detected_codec is not None and detected_codec != attempt_codec:
            attempt_codec = detected_codec
            parameter_cache.clear()
            got_keyframe = False
        codec = attempt_codec
        if codec is None:
            continue
        required = (7, 8) if codec == "h264" else (32, 33, 34)
        slices = []
        parameters_changed = False
        for nal in _iter_nal_units(frame_data):
            nal_type = (nal[0] & 0x1F) if codec == "h264" else ((nal[0] >> 1) & 0x3F)
            if nal_type in required:
                encoded = b"\x00\x00\x00\x01" + nal
                if parameter_cache.get(nal_type) != encoded:
                    parameters_changed = True
                parameter_cache[nal_type] = encoded
            else:
                slices.append(b"\x00\x00\x00\x01" + nal)
        if parameters_changed and got_keyframe:
            got_keyframe = False
        is_keyframe = _is_video_keyframe(frame_data, codec)
        has_slice = any(
            (1 <= header & 0x1F <= 5) if codec == "h264"
            else ((header >> 1) & 0x3F) <= 31
            for header in _annex_b_nal_headers(frame_data)
        )
        if not has_slice:
            continue
        if not got_keyframe and not is_keyframe:
            continue
        if any(kind not in parameter_cache for kind in required):
            continue
        parameter_sets = b"".join(parameter_cache[kind] for kind in required)
        if is_keyframe:
            frame_data = parameter_sets + b"".join(slices)
        if not got_keyframe:
            starts.append((codec, parameter_sets, frame_data))
            got_keyframe = True
        delivered.append((frame_data, is_keyframe))
    return delivered, starts


# Randomized access units.

H264_TYPES = (1, 1, 5, 5, 6, 7, 8, 9, 10, 12)
# TRAIL_N/R, STSA_N, IDR_W_RADL, IDR_N_LP, CRA, VPS/SPS/PPS, AUD, EOS, SEI.
HEVC_TYPES = (0, 1, 1, 4, 19, 20, 21, 32, 33, 34, 35, 36, 39)


def emulation_prevented(rbsp: bytes) -> bytes:
    """Insert emulation prevention bytes, as an encoder does."""
    out = bytearray()
    zeros = 0
    for byte in rbsp:
        if zeros >= 2 and byte <= 3:
            out.append(3)
            zeros = 0
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def random_payload(rng: random.Random) -> bytes:
    size = rng.choice((0, 1, 2, 3, rng.randrange(4, 64), rng.randrange(64, 600)))
    # Mostly 0-3, so start-code-like patterns are frequent.
    rbsp = bytes(rng.choice((0, 0, 0, 1, 2, 3, rng.randrange(256))) for _ in range(size))
    # Without emulation prevention a payload can contain a start code.
    return emulation_prevented(rbsp) if rng.random() < 0.7 else rbsp


def random_header(rng: random.Random, codec: str) -> bytes:
    if codec == "h264":
        nal_type = rng.choice((*H264_TYPES, rng.randrange(32)))
        return bytes([(rng.random() < 0.05) << 7 | rng.randrange(4) << 5 | nal_type])
    nal_type = rng.choice((*HEVC_TYPES, rng.randrange(64)))
    layer = 0 if rng.random() < 0.9 else rng.randrange(1, 64)
    tid = 1 if rng.random() < 0.8 else rng.randrange(8)  # 0 is invalid
    forbidden = (rng.random() < 0.05) << 7
    header = bytes([forbidden | nal_type << 1 | layer >> 5, (layer & 0x1F) << 3 | tid])
    return header[:1] if rng.random() < 0.05 else header


def nal_unit(rng: random.Random, header: bytes) -> bytes:
    # trailing_zero_8bits, or zeros an encoder pads a NAL unit with.
    return header + random_payload(rng) + bytes(rng.choice((0, 0, 0, 1, 2, 3)))


def annex_b(rng: random.Random, nal_units: list[bytes]) -> bytes:
    # Nothing, zeros or garbage before the first start code.
    out = bytearray(rng.choice((b"", b"", b"\x00", b"\x00\x00", bytes([rng.randrange(256)]))))
    for nal in nal_units:
        out += rng.choice((b"\x00\x00\x01", b"\x00\x00\x00\x01")) + nal
    if rng.random() < 0.1:
        out += rng.choice((b"\x00", b"\x00\x00", b"\x00\x00\x01", b"\x00\x00\x00\x01"))
    if rng.random() < 0.1:
        del out[rng.randrange(len(out) + 1):]
    return bytes(out)


def length_prefixed(rng: random.Random, nal_units: list[bytes]) -> bytes:
    """AVCC/HVCC: a 4-byte big-endian length before each NAL unit."""
    out = bytearray()
    for nal in nal_units:
        out += len(nal).to_bytes(4, "big") + nal
    roll = rng.random()
    if roll < 0.1:
        out += bytes(rng.randrange(1, 4))  # too short for another length
    elif roll < 0.2 and out:
        del out[rng.randrange(len(out)):]  # the last NAL unit truncated
    return bytes(out)


def access_unit(rng: random.Random, nal_units: list[bytes]) -> bytes:
    if rng.random() < 0.25:
        return length_prefixed(rng, nal_units)
    return annex_b(rng, nal_units)


def random_access_unit(rng: random.Random) -> bytes:
    codec = rng.choice(("h264", "hevc"))
    return access_unit(rng, [
        b"" if rng.random() < 0.05 else nal_unit(rng, random_header(rng, codec))
        for _ in range(rng.randrange(1, 7))
    ])


HEVC_PARAMETER_SETS = (
    b"\x00\x00\x00\x01\x40\x01\xaa" b"\x00\x00\x00\x01\x42\x01\xbb"
    b"\x00\x00\x00\x01\x44\x01\xcc"
)
EDGE_UNITS = (
    # HEVC IDR_N_LP, STSA_N and EOS headers share the H.264 PPS type bits.
    HEVC_PARAMETER_SETS + b"\x00\x00\x00\x01\x28\x01\xdd",
    b"\x00\x00\x00\x01\x08\x01\xee",
    b"\x00\x00\x00\x01\x02\x01\xee\x00\x00\x00\x01\x48\x01",
    b"\x00\x00\x01\x28\x00\xdd",  # IDR_N_LP header with an invalid tid 0
    b"\x00\x00\x00\x01\x67\x42\x00\x1e\x00\x00\x00\x01\x68\xce\x06\xe2\x00\x00\x01\x65\x88\x84",
    b"\x00\x00\x00\x01\x41\x9a\x21\x42",  # H.264 P slice resembling HEVC type 32
    b"", b"\x00", b"\x00\x00", b"\x00\x00\x01", b"\x00\x00\x00\x01",
    b"\x00\x00\x01\x00\x00\x01",
    b"\x00\x00\x01\x00\x00\x00\x00\x00\x01\x65",
    b"\x00\x00\x01\x65\x00\x00\x00",
    b"\x00\x00\x00\x02\x65\x88",  # AVCC
    b"\x00\x00\x00\x05\x65\x88",  # AVCC, truncated
)


@pytest.mark.parametrize("seed", range(4))
def test_single_pass_parser_matches_the_byte_scan(seed):
    rng = random.Random(seed)
    for raw in (*EDGE_UNITS, *(random_access_unit(rng) for _ in range(400))):
        # on_video parses the converted unit; the parser must also agree on
        # input the conversion left alone.
        for frame_data in {raw, module._convert_to_annex_b(raw)}:
            nal_units = module._split_nal_units(frame_data)
            assert nal_units == list(_iter_nal_units(frame_data)), frame_data
            assert module._detect_video_codec(nal_units) == _detect_video_codec(frame_data), frame_data
            for codec in ("h264", "hevc"):
                assert (
                    module._is_video_keyframe(nal_units, codec)
                    == _is_video_keyframe(frame_data, codec)
                ), (codec, frame_data)
                assert (
                    module._extract_parameter_sets(frame_data, codec)
                    == _extract_parameter_sets(frame_data, codec)
                ), (codec, frame_data)


PARAMETER_SET_HEADERS = {"h264": (b"\x67", b"\x68"), "hevc": (b"\x40\x01", b"\x42\x01", b"\x44\x01")}
# IDR, or IDR_W_RADL and IDR_N_LP.
KEYFRAME_HEADERS = {"h264": (b"\x65",), "hevc": (b"\x26\x01", b"\x28\x01")}
# P slices; TRAIL_R, TRAIL_N, STSA_N and CRA.
PREDICTED_HEADERS = {"h264": (b"\x41", b"\x01", b"\x21"), "hevc": (b"\x02\x01", b"\x00\x01", b"\x08\x01", b"\x2a\x01")}
# SEI, AUD and end of sequence.
OTHER_HEADERS = {"h264": (b"\x06", b"\x09", b"\x0a"), "hevc": (b"\x4e\x01", b"\x46\x01", b"\x48\x01")}


def random_stream(rng: random.Random) -> list[bytes]:
    """Codec segments of parameter sets, keyframes and predicted frames."""
    frames = []
    for _ in range(rng.randrange(2, 5)):
        codec = rng.choice(("h264", "hevc"))
        parameter_sets = [nal_unit(rng, header) for header in PARAMETER_SET_HEADERS[codec]]
        for _ in range(rng.randrange(10, 40)):
            roll = rng.random()
            if roll < 0.05:
                # A new format: on_video restarts the muxer.
                parameter_sets = [nal_unit(rng, header) for header in PARAMETER_SET_HEADERS[codec]]
            if roll < 0.1:
                frames.append(random_access_unit(rng))
                continue
            if roll < 0.2:
                units = list(parameter_sets)
            elif roll < 0.4:
                units = [
                    *(parameter_sets if rng.random() < 0.7 else ()),
                    nal_unit(rng, rng.choice(KEYFRAME_HEADERS[codec])),
                ]
            else:
                units = [nal_unit(rng, rng.choice(PREDICTED_HEADERS[codec]))]
            if rng.random() < 0.2:
                units.insert(
                    rng.randrange(len(units) + 1),
                    nal_unit(rng, rng.choice(OTHER_HEADERS[codec])),
                )
            frames.append(access_unit(rng, units))
    return frames


def run_on_video(monkeypatch, frames):
    """Feed one attempt's frames to the worker's real on_video callback."""
    bridge = make_bridge()
    delivered, starts = [], []
    monkeypatch.setattr(bridge, "_wait_for_prewake", lambda: True)
    monkeypatch.setattr(bridge, "_should_keep_running", lambda: True)

    def enqueue_video(data, *, reset_bootstrap=False):
        delivered.append((data, reset_bootstrap))
        return True

    def start_muxer(codec, *, parameter_sets=b"", first_keyframe=b"", **_):
        starts.append((codec, parameter_sets, first_keyframe))
        return True

    monkeypatch.setattr(bridge, "_enqueue_video", enqueue_video)
    monkeypatch.setattr(bridge, "_start_ffmpeg_muxer", start_muxer)

    def create_streamer(device, on_video, on_audio, on_login, on_disconnect,
                        video_id, manage_stream_switch=True):
        streamer = Mock(_cloudedge_native_signaling_retry=True)

        def run():
            for frame in frames:
                on_video(frame)
            bridge._running = False
            return len(frames), sum(map(len, frames))

        streamer.run_session.side_effect = run
        return streamer

    bridge._coordinator.client.create_streamer = create_streamer
    bridge._stream_worker(1)
    return delivered, starts


@pytest.mark.parametrize("seed", range(4))
def test_on_video_hands_on_what_the_byte_scan_did(monkeypatch, seed):
    frames = random_stream(random.Random(seed))
    expected_delivered, expected_starts = reference_on_video(frames)
    # The stream must reach every decision: a muxer start, keyframes
    # rebuilt with cached parameter sets, and predicted frames.
    assert expected_starts
    assert {keyframe for _, keyframe in expected_delivered} == {True, False}
    delivered, starts = run_on_video(monkeypatch, frames)
    assert starts == expected_starts
    assert delivered == expected_delivered
