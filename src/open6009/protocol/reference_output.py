"""Reference output plans and calibration codecs for NI USB-6009.

The digital value field is corroborated by bit captures. AO writes derive
DAC codes from calibration replies observed in NI Base 15 traffic.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from math import isfinite


def _b(value: str) -> bytes:
    return bytes.fromhex(value)


_DISCOVERY = _b("0000000c0008010000000000")
_DISCOVERY_REPLY = _b("0000001c0018010000000002020000000132c0000100000000020000")
_OK = _b("0000000c0008010000000002")


@dataclass(frozen=True, slots=True)
class CapturedOutputPlan:
    frames: tuple[bytes, ...]
    replies: tuple[bytes, ...]


def captured_ao_zero_plan(channel: int) -> CapturedOutputPlan:
    """The exact reference sequence that writes 0.0 V to AO0 or AO1."""

    return captured_ao_code_plan(channel, 0)


def captured_ao_code_plan(channel: int, code: int) -> CapturedOutputPlan:
    """Encode a 12-bit code after checking this device's calibration replies."""

    if channel not in (0, 1):
        raise ValueError("AO channel must be 0 or 1")
    if not isinstance(code, int) or not 0 <= code <= 0xFFF:
        raise ValueError("AO code must be a 12-bit integer")
    selectors = (0x54, 0x50) if channel == 0 else (0x5C, 0x58)
    calibration = (
        ("000000140010010000000002000640a0fe433bd4",
         "00000014001001000000000200063b541b8740a0"),
        ("000000140010010000000002000640a190abffff",
         "00000014001001000000000200063bd4735d40a1"),
    )[channel]
    frames = (_DISCOVERY, _DISCOVERY) + tuple(
        _b(f"000000140010011402040000000001{selector:02x}00040000") for selector in selectors
    ) + (_b(f"00000010000c010e0221000{channel}{code:04x}0000"),)
    return CapturedOutputPlan(frames, (_DISCOVERY_REPLY, _DISCOVERY_REPLY,
                                       _b(calibration[0]), _b(calibration[1]), _OK))


_AO_CAL_PREFIX = _b("0000001400100100000000020006")


@dataclass(frozen=True, slots=True)
class AoCalibration:
    zero_code_voltage: float
    full_code_voltage: float
    zero_reply: bytes
    full_reply: bytes


def decode_ao_calibration_replies(zero_reply: bytes, full_reply: bytes) -> AoCalibration:
    """Read the two NI calibration endpoints from 0x0150/0x0154 replies."""

    if (len(zero_reply) != 20 or len(full_reply) != 20
            or not zero_reply.startswith(_AO_CAL_PREFIX)
            or not full_reply.startswith(_AO_CAL_PREFIX)):
        raise ValueError("invalid AO calibration reply envelope")
    zero = struct.unpack(">f", zero_reply[14:18])[0]
    full = struct.unpack(">f", full_reply[14:18])[0]
    if not isfinite(zero) or not isfinite(full) or not -0.5 <= zero <= 0.5 or not 4.5 <= full <= 5.5:
        raise ValueError("unexpected AO calibration endpoints")
    return AoCalibration(zero, full, zero_reply, full_reply)


def ao_voltage_to_code(voltage: float, calibration: AoCalibration) -> int:
    """Reproduce NI Base 15's 12-bit DAC code from calibrated endpoints."""

    if not isinstance(voltage, (int, float)) or not isfinite(voltage) or not 0 <= voltage <= 5:
        raise ValueError("AO voltage must be finite and within 0..5 V")
    code = round((voltage - calibration.zero_code_voltage) * 4095 /
                 (calibration.full_code_voltage - calibration.zero_code_voltage))
    return max(0, min(4095, code))


def reference_ao_voltage_code(channel: int, voltage: float) -> int:
    """Map 0–5 V using this serial's captured calibration replies."""

    plan = captured_ao_zero_plan(channel)
    calibration = decode_ao_calibration_replies(plan.replies[3], plan.replies[2])
    return ao_voltage_to_code(voltage, calibration)


def reference_ao_voltage_plan(channel: int, voltage: float) -> CapturedOutputPlan:
    """Build an AO plan using this serial's captured calibration endpoints."""

    return captured_ao_code_plan(channel, reference_ao_voltage_code(channel, voltage))


def captured_digital_zero_plan(port: int) -> CapturedOutputPlan:
    """The exact NI reference sequence that drives a full digital port low."""

    return captured_digital_port_plan(port, 0)


def captured_digital_port_plan(port: int, value: int) -> CapturedOutputPlan:
    """Encode a full-port value using NI captures of zero and individual bits.

    Observed single-bit references establish that the final frame's byte 16
    holds the desired port value. This does not establish electrical level or
    persistence after task cleanup.
    """

    if port not in (0, 1):
        raise ValueError("digital port must be 0 or 1")
    maximum = 0xFF if port == 0 else 0x0F
    if not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"digital port {port} value must be 0..{maximum}")
    port_byte = f"{port:02x}"
    frames = (
        _DISCOVERY,
        _b(f"00000010000c010e021000000003{port_byte}00"),
        _b(f"000000140010010f021000000003{port_byte}0003ff0000"),
        _b(f"0000001400100112021000000004{'ff00' if port == 0 else '00ff'}00040000"),
        _b(f"000000140010010f021000000003{port_byte}0003{value:02x}0000"),
    )
    replies = (_DISCOVERY_REPLY, _b("00000010000c0100000000020003ff00"),
               _OK, _OK, _OK)
    return CapturedOutputPlan(frames, replies)
