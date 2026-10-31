"""Offline reconstruction of the captured NI Base-15 single-AI finite plan.

This module encodes only values established by the successful reference
captures: AI0/RSE, the observed +/-10 V request, finite samples, and the
24 MHz sample-rate divisor. It deliberately performs no USB I/O and must not
be treated as authorization to replay the sequence on a physical device.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

_RATE_CLOCK_HZ = 24_000_000

# Exact Base-15 EP1 OUT frames from the timestamped 100 S/s, 100 sample AI0
# reference trace. The three mutable payloads are replaced only where
# independent one-variable captures establish their meaning.
_AI0_REFERENCE_FRAMES = tuple(
    bytes.fromhex(value)
    for value in (
        "0000000c0008010000000000",
        "0000000c0008010b02000000",
        "0000000c0008010c02000000",
        "0000000c0008010d02000000",
        "0000000c0008010c02030000",
        "00000010000c010f0202000000040000",
        "00000010000c010e0202000000030200",
        "0000001400100114020400000000011000080000",
        "0000001c001801100202000000002710ffffd8f0fdfd000400000000",
        "00000010000c010e0200000001000000",
        "00000010000c0116020000000003a980",
        "0000000c0008011302000000",
        "0000000c0008011502000000",
        "00000010000c010f0200000000000064",
        "00000010000c011202030000000000c8",
        "0000000c0008010f02030000",
        "0000000c0008010902030000",
        "0000000c0008010902000000",
        "0000000c0008010a02000000",
        "0000000c0008011002030000",
        "0000000c0008011002030000",
        "0000000c0008010b02000000",
        "0000000c0008010c02000000",
        "0000000c0008010c02030000",
        "0000000c0008010d02000000",
    )
)

_GENERIC_REPLY = bytes.fromhex("0000000c0008010000000002")
_REFERENCE_REPLIES = tuple(
    bytes.fromhex(value)
    for value in (
        "0000001c0018010000000002020000000132c0000100000000020000",
        *(_GENERIC_REPLY.hex() for _ in range(6)),
        "000000180014010000000002000ac1246be64129c01dc124",
        *(_GENERIC_REPLY.hex() for _ in range(17)),
    )
)

# Per-channel response examples at frame seven. Runtime acquisition accepts and
# validates the live device's calibration reply rather than requiring these
# particular coefficient bytes.
_CHANNEL_PROGRAM_REPLIES = tuple(
    bytes.fromhex(value)
    for value in (
        "000000180014010000000002000ac1246be64129c01dc124",
        "000000180014010000000002000ac12440a94129c22cc124",
        "000000180014010000000002000ac1245bed4129cd8dc124",
        "000000180014010000000002000ac1245b144129bc88c124",
        "000000180014010000000002000ac124601c4129c49dc124",
        "000000180014010000000002000ac1245bfe4129bdafc124",
        "000000180014010000000002000ac12464d54129c8e7c124",
        "000000180014010000000002000ac12459bb4129beb53b54",
    )
)

_DIFFERENTIAL_RANGES_DESCENDING = (20.0, 10.0, 5.0, 4.0, 2.5, 2.0, 1.25, 1.0)
DIFFERENTIAL_CALIBRATION_REPLY_PREFIX = bytes.fromhex("000000180014010000000002000a")


@dataclass(frozen=True, slots=True)
class CapturedAiFinitePlan:
    """A transport-free EP1 plan and expected raw EP2 byte count."""

    sample_rate_hz: int
    sample_count: int
    channel: int
    ep1_out_frames: tuple[bytes, ...]
    expected_ep1_replies: tuple[bytes, ...]

    @property
    def expected_raw_bytes(self) -> int:
        return self.sample_count * 2


def captured_ai0_rse_finite_plan(
    *, sample_rate_hz: int, sample_count: int, channel: int = 0
) -> CapturedAiFinitePlan:
    """Rebuild the observed AI0 finite plan for captured-supported values.

    Captures at 100 and 200 S/s prove qualifier ``0x0116`` receives a
    big-endian ``24_000_000 / rate`` divisor. Captures at 100 and 50 samples
    prove qualifier ``0x010f`` receives the finite count and qualifier
    ``0x0112`` receives twice that count. All remaining bytes are exact fixed
    AI0/RSE/+/-10 V reference bytes with unresolved semantics.
    """

    if not 0 <= channel <= 7:
        raise ValueError("channel must be in the captured RSE range 0..7")
    if not 1 <= sample_count <= 0xFFFFFFFF:
        raise ValueError("sample_count must fit in an unsigned 32-bit field")
    if sample_rate_hz <= 0 or _RATE_CLOCK_HZ % sample_rate_hz:
        raise ValueError("sample_rate_hz must be a positive divisor of 24 MHz")
    divisor = _RATE_CLOCK_HZ // sample_rate_hz
    if divisor > 0xFFFFFFFF:
        raise ValueError("sample_rate_hz produces an out-of-range timer divisor")
    frames = list(_AI0_REFERENCE_FRAMES)
    frames[10] = frames[10][:-4] + struct.pack(">I", divisor)
    frames[13] = frames[13][:-4] + struct.pack(">I", sample_count)
    frames[14] = frames[14][:-4] + struct.pack(">I", sample_count * 2)
    # Captures for every RSE channel establish this mux/scan field table:
    # 0..3 => 0x1100..0x1400; 4..7 => 0x1180..0x1480.
    frames[5] = frames[5][:-1] + bytes((channel,))
    scan_field = 0x11000 + ((channel % 4) << 12) + (0x800 if channel >= 4 else 0)
    frames[7] = frames[7][:14] + scan_field.to_bytes(3, "big") + frames[7][17:]
    replies = list(_REFERENCE_REPLIES)
    replies[7] = _CHANNEL_PROGRAM_REPLIES[channel]
    return CapturedAiFinitePlan(sample_rate_hz, sample_count, channel, tuple(frames), tuple(replies))


def captured_differential_finite_plan(
    *, channel: int, voltage_range: float, sample_rate_hz: int, sample_count: int
) -> CapturedAiFinitePlan:
    """Encode the captured differential mode and gain-selector fields.

    AI0 was captured at all eight ranges; AI1..3 were captured at +/-10 V.
    The selector's additive channel/range structure is supported by those
    traces. Calibration payloads vary, so the caller must validate their
    header and retain the returned bytes rather than compare a fixed value.
    """

    if not 0 <= channel <= 3:
        raise ValueError("differential channel must be ai0..ai3")
    if voltage_range not in _DIFFERENTIAL_RANGES_DESCENDING:
        raise ValueError("unsupported differential range")
    base = captured_ai0_rse_finite_plan(
        channel=channel, sample_rate_hz=sample_rate_hz, sample_count=sample_count
    )
    frames = list(base.ep1_out_frames)
    frames[6] = frames[6][:-2] + b"\x00\x00"
    selector = 0x10 + 0x20 * _DIFFERENTIAL_RANGES_DESCENDING.index(voltage_range) + 8 * channel
    frames[7] = frames[7][:13] + selector.to_bytes(3, "big") + frames[7][16:]
    millivolts = int(voltage_range * 1000)
    frames[8] = (frames[8][:12] + millivolts.to_bytes(4, "big", signed=True)
                 + (-millivolts).to_bytes(4, "big", signed=True) + frames[8][20:])
    replies = list(base.expected_ep1_replies)
    replies[7] = DIFFERENTIAL_CALIBRATION_REPLY_PREFIX
    return CapturedAiFinitePlan(sample_rate_hz, sample_count, channel,
                                tuple(frames), tuple(replies))
