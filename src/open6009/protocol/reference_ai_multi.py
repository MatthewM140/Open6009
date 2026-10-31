"""Finite multichannel RSE plans reconstructed from NI Base 15 captures."""

from __future__ import annotations

from dataclasses import dataclass

from .reference_ai import _GENERIC_REPLY, captured_ai0_rse_finite_plan


def _frame(qualifier: int, payload: bytes) -> bytes:
    length = 12 + len(payload)
    padding = (-length) % 4
    length += padding
    return (length.to_bytes(4, "big") + (length - 4).to_bytes(2, "big")
            + bytes((1, qualifier)) + b"\x02\x02\x00\x00" + payload + bytes(padding))


@dataclass(frozen=True, slots=True)
class CapturedAiMultiPlan:
    channels: tuple[int, ...]
    sample_rate_hz: int
    sample_count: int
    ep1_out_frames: tuple[bytes, ...]
    expected_ep1_replies: tuple[bytes, ...]

    @property
    def ep2_before_frame(self) -> int:
        return 18 + 2 * len(self.channels)

    @property
    def expected_raw_bytes(self) -> int:
        return 2 * len(self.channels) * self.sample_count


def captured_rse_multichannel_plan(
    channels: tuple[int, ...], *, sample_rate_hz: int, sample_count: int
) -> CapturedAiMultiPlan:
    """Build the observed 2–8 channel finite RSE plan.

    NI traces prove list packing for 2, 3, 4, 5, and 8 channels, including
    noncontiguous AI0+AI2. Rates/counts use the single-channel captured fields.
    Other combinations are an evidence-supported generalization.
    """

    if not 2 <= len(channels) <= 8 or len(set(channels)) != len(channels):
        raise ValueError("multichannel plan requires 2..8 unique channels")
    if any(not 0 <= channel <= 7 for channel in channels):
        raise ValueError("RSE channels must be ai0..ai7")
    if sample_rate_hz <= 0:
        raise ValueError("sample rate must be positive")
    if sample_count > 4096 or sample_count / sample_rate_hz > 10:
        raise ValueError("finite acquisition is bounded to 4096 scans and 10 seconds")
    single = captured_ai0_rse_finite_plan(sample_rate_hz=sample_rate_hz,
                                           sample_count=sample_count)
    frames = list(single.ep1_out_frames[:5])
    replies = list(single.expected_ep1_replies[:5])
    frames.append(_frame(0x0F, (2 * len(channels) + 2).to_bytes(2, "big")
                         + b"".join(channel.to_bytes(2, "big") for channel in channels)))
    frames.append(_frame(0x0E, (len(channels) + 2).to_bytes(2, "big")
                         + b"\x02" * len(channels)))
    replies.extend((_GENERIC_REPLY, _GENERIC_REPLY))
    for scan_index, channel in enumerate(channels):
        program = captured_ai0_rse_finite_plan(sample_rate_hz=sample_rate_hz,
                                                sample_count=sample_count, channel=channel)
        frames.append(program.ep1_out_frames[7])
        frames.append(single.ep1_out_frames[8][:-4]
                      + scan_index.to_bytes(2, "big") + single.ep1_out_frames[8][-2:])
        replies.extend((program.expected_ep1_replies[7], _GENERIC_REPLY))
    tail = list(single.ep1_out_frames[9:])
    tail[5] = tail[5][:-4] + (2 * len(channels) * sample_count).to_bytes(4, "big")
    frames.extend(tail)
    replies.extend(single.expected_ep1_replies[9:])
    return CapturedAiMultiPlan(channels, sample_rate_hz, sample_count,
                               tuple(frames), tuple(replies))
