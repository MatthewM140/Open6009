"""GUI acquisition configuration and display-only data structures."""

from __future__ import annotations

from dataclasses import dataclass

from open6009.acquisition.config import AnalogInputConfig, TerminalMode


@dataclass(frozen=True)
class AcquisitionSettings:
    channels: tuple[int, ...]
    mode: TerminalMode = TerminalMode.RSE
    voltage_range: float = 10.0
    rate_per_channel: int = 1000
    finite_scans: int | None = None
    display_seconds: float = 5.0
    record_path: str | None = None
    digital_ports: tuple[int, ...] = ()
    digital_poll_hz: int = 2
    digital_generation: int = 0

    def validate(self) -> None:
        # The finite backend has a 4096-sample, 10-second bound. Continuous
        # acquisition currently supports RSE +/-10 V only.
        count = self.finite_scans or 100
        AnalogInputConfig(self.channels, self.mode, self.voltage_range,
                          count, self.rate_per_channel)
        if self.finite_scans is None and (
            self.mode is not TerminalMode.RSE or self.voltage_range != 10.0
        ):
            raise ValueError("continuous acquisition supports RSE +/-10 V only")
        if self.finite_scans is not None:
            if self.finite_scans > 4096 or self.finite_scans / self.rate_per_channel > 10:
                raise ValueError("finite acquisition is limited to 4096 scans and 10 seconds")
            if len(self.channels) > 1 and (
                self.mode is not TerminalMode.RSE or self.voltage_range != 10.0
            ):
                raise ValueError("multichannel finite acquisition supports RSE +/-10 V only")
        if not 0.1 <= self.display_seconds <= 60:
            raise ValueError("display window must be 0.1..60 seconds")
        if (any(type(port) is not int or port not in (0, 1) for port in self.digital_ports)
                or len(set(self.digital_ports)) != len(self.digital_ports)):
            raise ValueError("digital ports must be unique port 0 or 1 selectors")
        if type(self.digital_poll_hz) is not int or not 1 <= self.digital_poll_hz <= 10:
            raise ValueError("digital polling must be 1..10 reads/s/port")
        if self.digital_ports and self.finite_scans is not None:
            raise ValueError("combined analog and digital acquisition requires continuous mode")

    @property
    def aggregate_rate(self) -> int:
        return self.rate_per_channel * len(self.channels)


@dataclass(frozen=True)
class PlotBatch:
    channels: tuple[int, ...]
    times: tuple[float, ...]
    voltages: tuple[tuple[float, ...], ...]
    scans_total: int
    raw_bytes_total: int
    elapsed_seconds: float
    first_channel_volts_min_mean_max: tuple[float, float, float] | None = None
