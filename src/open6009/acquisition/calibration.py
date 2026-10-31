"""NI-reference-equivalent AI voltage conversion from captured calibration replies.

Captured NI Base 15 raw EP2 words and reported F64 values establish the exact
affine conversions for RSE and differential input. Electrical accuracy has
not been independently checked with a known voltage source.
"""

from __future__ import annotations

import math
import struct
import sys
from array import array
from dataclasses import dataclass

from .config import TerminalMode

_CALIBRATION_PREFIX = bytes.fromhex("000000180014010000000002000a")


@dataclass(frozen=True, slots=True)
class AiCalibration:
    lower_voltage: float
    upper_voltage: float
    raw_reply: bytes


@dataclass(frozen=True, slots=True)
class CalibratedAiBatch:
    channels: tuple[int, ...]
    raw_bytes: bytes
    calibrations: tuple[AiCalibration, ...]
    scans_volts: tuple[tuple[float, ...], ...]

    @property
    def samples_per_channel(self) -> int:
        return len(self.scans_volts)


def decode_ai_calibration_reply(reply: bytes) -> AiCalibration:
    """Decode two big-endian IEEE-754 endpoints from a captured 0x0204 reply."""

    if len(reply) != 24 or not reply.startswith(_CALIBRATION_PREFIX):
        raise ValueError("invalid AI calibration reply envelope")
    lower, upper = struct.unpack(">ff", reply[14:22])
    if not math.isfinite(lower) or not math.isfinite(upper) or lower >= upper:
        raise ValueError("invalid AI calibration endpoints")
    return AiCalibration(lower, upper, reply)


def ni_reference_voltage(raw_word: int, mode: TerminalMode, calibration: AiCalibration) -> float:
    """Reproduce NI Base F64 conversion for one big-endian EP2 word."""

    if not 0 <= raw_word <= 0xFFFF:
        raise ValueError("raw AI word must be unsigned 16-bit")
    span = calibration.upper_voltage - calibration.lower_voltage
    if mode is TerminalMode.RSE:
        return calibration.lower_voltage + (2 * raw_word - 65536) * span / 65535
    if mode is TerminalMode.DIFFERENTIAL:
        return calibration.lower_voltage + raw_word * span / 65535
    raise ValueError("unsupported AI terminal mode")


def decode_calibrated_scans(
    raw: bytes,
    channels: tuple[int, ...],
    mode: TerminalMode,
    calibration_replies: tuple[bytes, ...],
) -> CalibratedAiBatch:
    """Decode scan-major EP2 words using per-channel NI coefficients."""

    if not channels or len(channels) != len(calibration_replies):
        raise ValueError("one calibration reply is required per channel")
    stride = 2 * len(channels)
    if len(raw) % stride:
        raise ValueError("EP2 data does not contain complete scans")
    calibrations = tuple(decode_ai_calibration_reply(reply) for reply in calibration_replies)
    words = array("H")
    words.frombytes(raw)
    if sys.byteorder == "little":
        words.byteswap()
    lowers = tuple(calibration.lower_voltage for calibration in calibrations)
    spans = tuple(calibration.upper_voltage - calibration.lower_voltage for calibration in calibrations)
    channel_count = len(channels)
    scans: list[tuple[float, ...]] = []
    if mode is TerminalMode.RSE:
        for offset in range(0, len(words), channel_count):
            scans.append(tuple(
                lowers[slot] + (2 * words[offset + slot] - 65536) * spans[slot] / 65535
                for slot in range(channel_count)
            ))
    elif mode is TerminalMode.DIFFERENTIAL:
        for offset in range(0, len(words), channel_count):
            scans.append(tuple(
                lowers[slot] + words[offset + slot] * spans[slot] / 65535
                for slot in range(channel_count)
            ))
    else:
        raise ValueError("unsupported AI terminal mode")
    return CalibratedAiBatch(channels, raw, calibrations, tuple(scans))
