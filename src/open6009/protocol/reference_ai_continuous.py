"""Captured continuous RSE command schedules.

NI Base 15 traces at 1 and 10 kS/s, three 100-sample reads each, establish
the setup, per-chunk commands, and stop tail. This codec performs no USB I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from .reference_ai import captured_ai0_rse_finite_plan
from .reference_ai_multi import captured_rse_multichannel_plan


@dataclass(frozen=True, slots=True)
class ContinuousAiPlan:
    channels: tuple[int, ...]
    sample_rate_hz: int
    start_frames: tuple[bytes, ...]
    start_replies: tuple[bytes, ...]
    stop_frames: tuple[bytes, ...]
    stop_replies: tuple[bytes, ...]

    def read_frames(self, chunk_index: int, samples: int = 100) -> tuple[bytes, bytes, bytes]:
        if chunk_index < 0 or samples not in (100, 250, 500, 1000):
            raise ValueError("invalid continuous read index or sample count")
        return self.read_frames_for_total((chunk_index + 1) * samples)

    def read_frames_for_total(self, total_scans: int) -> tuple[bytes, bytes, bytes]:
        """Encode the captured credit field from cumulative requested scans."""

        if total_scans <= 0:
            raise ValueError("total scans must be positive")
        remaining = 0x7FFFFFFF - total_scans * 2 * len(self.channels)
        if remaining < 0:
            raise ValueError("continuous byte counter exhausted")
        credit = bytes.fromhex("00000010000c011102030000") + remaining.to_bytes(4, "big")
        status = bytes.fromhex("0000000c0008011002030000")
        return credit, status, status


def captured_continuous_ai0_plan(sample_rate_hz: int) -> ContinuousAiPlan:
    """Build the observed AI0/RSE continuous setup and cleanup plan."""

    return captured_continuous_rse_plan((0,), sample_rate_hz)


def captured_continuous_rse_plan(
    channels: tuple[int, ...], sample_rate_hz: int
) -> ContinuousAiPlan:
    """Build the NI-captured continuous RSE setup for one to eight channels.

    NI traces directly cover one, two, and four channels. Other channel sets
    use the independently captured finite channel-list and programming format.
    """

    if not channels or len(channels) > 8 or len(set(channels)) != len(channels):
        raise ValueError("continuous AI requires 1..8 unique channels")
    if any(channel not in range(8) for channel in channels):
        raise ValueError("continuous RSE channels must be ai0..ai7")
    if sample_rate_hz * len(channels) > 48_000:
        raise ValueError("aggregate sample rate exceeds 48 kS/s")
    if len(channels) == 1:
        base = captured_ai0_rse_finite_plan(
            sample_rate_hz=sample_rate_hz, sample_count=100, channel=channels[0]
        )
    else:
        base = captured_rse_multichannel_plan(
            channels, sample_rate_hz=sample_rate_hz, sample_count=100
        )
    start_length = 17 + 2 * len(channels)
    frames = list(base.ep1_out_frames[:start_length])
    frames[7 + 2 * len(channels)] = frames[7 + 2 * len(channels)][:-4] + b"\x02\x00\x00\x00"
    frames[11 + 2 * len(channels)] = frames[11 + 2 * len(channels)][:-4] + bytes(4)
    frames[12 + 2 * len(channels)] = frames[12 + 2 * len(channels)][:-4] + (0x7FFFFFFF).to_bytes(4, "big")
    return ContinuousAiPlan(channels, sample_rate_hz, tuple(frames),
                            base.expected_ep1_replies[:start_length],
                            base.ep1_out_frames[-4:],
                            base.expected_ep1_replies[-4:])
