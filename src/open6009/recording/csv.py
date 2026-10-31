"""Lossless raw-code CSV output with explicit calibration state."""

from __future__ import annotations

import csv
from collections.abc import Iterable
from typing import TextIO

from ..acquisition.samples import SampleBatch

FIELDS = ("sample_index", "monotonic_ns", "channel", "raw_code", "volts", "calibrated")


def write_sample_batches(stream: TextIO, batches: Iterable[SampleBatch]) -> int:
    writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    count = 0
    for batch in batches:
        for row in batch.rows():
            writer.writerow(row)
            count += 1
    return count
