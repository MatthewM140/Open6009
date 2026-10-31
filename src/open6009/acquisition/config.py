"""Validated USB-6009 analog-input requests independent of USB encoding."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite

MAX_AGGREGATE_RATE = 48_000.0
DIFFERENTIAL_RANGES = frozenset({1.0, 1.25, 2.0, 2.5, 4.0, 5.0, 10.0, 20.0})


class TerminalMode(str, Enum):
    RSE = "rse"
    DIFFERENTIAL = "differential"


@dataclass(frozen=True)
class AnalogInputConfig:
    channels: tuple[int, ...]
    mode: TerminalMode
    voltage_range: float
    samples_per_channel: int
    rate_per_channel: float

    def __post_init__(self) -> None:
        if not self.channels:
            raise ValueError("at least one analog channel is required")
        if len(set(self.channels)) != len(self.channels):
            raise ValueError("analog channels must be unique")
        maximum_channel = 7 if self.mode is TerminalMode.RSE else 3
        if any(channel < 0 or channel > maximum_channel for channel in self.channels):
            raise ValueError(f"{self.mode.value} channels must be ai0..ai{maximum_channel}")
        if self.mode is TerminalMode.RSE and self.voltage_range != 10.0:
            raise ValueError("USB-6009 RSE input supports only the +/-10 V range")
        if self.mode is TerminalMode.DIFFERENTIAL and self.voltage_range not in DIFFERENTIAL_RANGES:
            allowed = ", ".join(f"+/-{value:g} V" for value in sorted(DIFFERENTIAL_RANGES))
            raise ValueError(f"unsupported differential range; choose {allowed}")
        if not isinstance(self.samples_per_channel, int) or self.samples_per_channel <= 0:
            raise ValueError("samples per channel must be positive")
        if not isinstance(self.rate_per_channel, (int, float)) or not isfinite(self.rate_per_channel) or self.rate_per_channel <= 0:
            raise ValueError("sample rate must be finite and positive")
        if self.aggregate_rate > MAX_AGGREGATE_RATE:
            raise ValueError(
                f"aggregate rate {self.aggregate_rate:g} S/s exceeds USB-6009 maximum "
                f"{MAX_AGGREGATE_RATE:g} S/s"
            )

    @property
    def aggregate_rate(self) -> float:
        return self.rate_per_channel * len(self.channels)

    @classmethod
    def one_channel(
        cls,
        channel: int,
        mode: TerminalMode,
        voltage_range: float,
        samples: int,
        rate: float,
    ) -> AnalogInputConfig:
        return cls((channel,), mode, voltage_range, samples, rate)
