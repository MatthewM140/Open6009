"""Raw-first sample representation used before calibration is understood."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SampleBatch:
    channel: int
    raw_codes: tuple[int, ...]
    monotonic_start_ns: int
    rate_hz: float
    volts: tuple[float, ...] | None = None
    calibrated: bool = False

    def __post_init__(self) -> None:
        if self.channel < 0:
            raise ValueError("channel cannot be negative")
        if self.rate_hz <= 0:
            raise ValueError("rate must be positive")
        if self.volts is not None and len(self.volts) != len(self.raw_codes):
            raise ValueError("raw and voltage sample counts differ")
        if self.calibrated and self.volts is None:
            raise ValueError("calibrated batch requires voltage values")

    def rows(self):
        step_ns = 1_000_000_000.0 / self.rate_hz
        for index, raw in enumerate(self.raw_codes):
            yield {
                "sample_index": index,
                "monotonic_ns": round(self.monotonic_start_ns + index * step_ns),
                "channel": self.channel,
                "raw_code": raw,
                "volts": None if self.volts is None else self.volts[index],
                "calibrated": self.calibrated,
            }
