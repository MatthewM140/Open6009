"""Spool raw data during USB I/O, then convert to CSV after acquisition stops."""

from __future__ import annotations

import csv
from pathlib import Path
from queue import Full, Queue
from threading import Thread

from open6009.acquisition.calibration import CalibratedAiBatch, decode_calibrated_scans
from open6009.acquisition.config import TerminalMode


class CsvRecorder:
    def __init__(self, path: Path, channels: tuple[int, ...], rate: int,
                 mode: TerminalMode) -> None:
        partial_path = path.with_name(path.name + ".partial")
        raw_partial_path = path.with_name(path.name + ".raw.partial")
        raw_path = path.with_suffix(".raw")
        for candidate in (path, partial_path, raw_partial_path, raw_path):
            if candidate.exists():
                raise FileExistsError(f"recording or incomplete capture already exists: {candidate}")
        if not path.parent.is_dir():
            raise FileNotFoundError(f"recording directory does not exist: {path.parent}")
        self.path = path
        self.partial_path = partial_path
        self.raw_partial_path = raw_partial_path
        self.raw_path = raw_path
        self.channels = channels
        self.rate = rate
        self.mode = mode
        self.calibration_replies: tuple[bytes, ...] | None = None
        self.queue: Queue[bytes | None] = Queue(maxsize=64)
        self.error: Exception | None = None
        self.scans = 0
        self.thread = Thread(target=self._spool, name="open6009-record-spool", daemon=True)
        self.thread.start()

    def _spool(self) -> None:
        try:
            with self.raw_partial_path.open("wb") as handle:
                while (raw := self.queue.get()) is not None:
                    handle.write(raw)
                    self.scans += len(raw) // (2 * len(self.channels))
        except Exception as exc:  # noqa: BLE001 - return writer failure across thread boundary
            self.error = exc

    def append(self, batch: CalibratedAiBatch) -> None:
        if self.error is not None:
            raise RuntimeError(f"recording failed: {self.error}") from self.error
        if batch.channels != self.channels:
            raise ValueError("recorded channel order changed during acquisition")
        replies = tuple(calibration.raw_reply for calibration in batch.calibrations)
        if self.calibration_replies is None:
            self.calibration_replies = replies
        elif self.calibration_replies != replies:
            raise ValueError("recorded calibration changed during acquisition")
        try:
            self.queue.put(batch.raw_bytes, timeout=0.1)
        except Full as exc:
            raise RuntimeError("raw recording spool could not keep up; capture stopped") from exc

    def _convert_csv(self) -> None:
        if self.calibration_replies is None:
            raise RuntimeError("cannot finalize a recording without calibration data")
        stride = 2 * len(self.channels)
        with self.raw_partial_path.open("rb") as source, self.partial_path.open(
            "w", newline=""
        ) as destination:
            writer = csv.writer(destination)
            writer.writerow(("time_s", *(f"ai{channel}_V" for channel in self.channels)))
            scan = 0
            while raw := source.read(4096 * stride):
                batch = decode_calibrated_scans(raw, self.channels, self.mode,
                                                self.calibration_replies)
                for values in batch.scans_volts:
                    writer.writerow((format(scan / self.rate, ".9f"),
                                     *(format(value, ".17g") for value in values)))
                    scan += 1
            if scan != self.scans:
                raise RuntimeError("recording scan count changed during CSV conversion")

    def finish(self, *, success: bool) -> None:
        # A writer error may leave the queue full; never wait forever here.
        while self.thread.is_alive():
            try:
                self.queue.put(None, timeout=0.1)
                break
            except Full:
                if self.error is not None:
                    break
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("recording writer did not stop")
        if self.error is not None:
            raise RuntimeError(f"recording failed: {self.error}") from self.error
        if success:
            self._convert_csv()
            self.raw_partial_path.replace(self.raw_path)
            self.partial_path.replace(self.path)
