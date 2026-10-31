"""Versioned, hardware-inert V2 workspace and captured-data files."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections import deque
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from .workspace import Workspace

MAX_MANIFEST = 1_000_000
MAX_RECORDING = 512 * 1024 * 1024


@dataclass(frozen=True)
class RecordingPreview:
    channels: tuple[int, ...]
    times: tuple[float, ...]
    volts: tuple[tuple[float, ...], ...]


def _atomic_target(path: Path) -> tuple[int, Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    return descriptor, Path(name)


def _commit(temp: Path, destination: Path) -> None:
    with temp.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temp, destination)
    directory = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def save_workspace(path: Path, workspace: Workspace, acquisition: dict[str, object]) -> None:
    """Save configuration only. Output values and armed states are never serialized."""

    if path.suffix != ".o6w":
        raise ValueError("workspace filename must end in .o6w")
    payload = {"schema": 1, "workspace": workspace.to_dict(), "acquisition": acquisition}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_MANIFEST:
        raise ValueError("workspace is too large")
    descriptor, temp = _atomic_target(path)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        _commit(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def save_project(path: Path, workspace: Workspace, acquisition: dict[str, object],
                 *, recording_csv: Path | None = None,
                 source_project: Path | None = None) -> None:
    """Save a self-contained project with optional completed CSV and raw sidecar."""

    if path.suffix != ".o6p":
        raise ValueError("project filename must end in .o6p")
    if recording_csv is not None and source_project is not None:
        raise ValueError("choose one recording source")
    recording = None
    raw = None
    if recording_csv is not None:
        raw = recording_csv.with_suffix(".raw")
        if not recording_csv.is_file() or not raw.is_file():
            raise ValueError("a completed CSV and raw sidecar are required")
        if recording_csv.stat().st_size > MAX_RECORDING or raw.stat().st_size > MAX_RECORDING:
            raise ValueError("recording exceeds project size limit")
        recording = {
            "csv_sha256": _sha256(recording_csv),
            "raw_sha256": _sha256(raw),
            "csv_bytes": recording_csv.stat().st_size,
            "raw_bytes": raw.stat().st_size,
        }
    if source_project is not None:
        load_project(source_project)
        with ZipFile(source_project) as previous:
            recording = json.loads(previous.read("manifest.json"))["recording"]
    manifest = {
        "schema": 1, "workspace": workspace.to_dict(),
        "acquisition": acquisition, "recording": recording,
    }
    encoded = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_MANIFEST:
        raise ValueError("project manifest is too large")
    descriptor, temp = _atomic_target(path)
    try:
        with os.fdopen(descriptor, "wb") as stream, ZipFile(stream, "w", ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", encoded)
            if recording_csv is not None and raw is not None:
                archive.write(recording_csv, "capture.csv")
                archive.write(raw, "capture.raw")
            elif source_project is not None and recording is not None:
                with ZipFile(source_project) as previous:
                    for member in ("capture.csv", "capture.raw"):
                        with previous.open(member) as source, archive.open(member, "w") as target:
                            shutil.copyfileobj(source, target, length=1024 * 1024)
        _commit(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def load_workspace(path: Path) -> tuple[Workspace, dict[str, object]]:
    if path.stat().st_size > MAX_MANIFEST:
        raise ValueError("workspace is too large")
    data = json.loads(path.read_text(encoding="utf-8"))
    return _validated_manifest(data, project=False)


def export_recording_csv(destination: Path, *, csv_path: Path | None = None,
                         project_path: Path | None = None) -> None:
    if (csv_path is None) == (project_path is None):
        raise ValueError("choose one completed recording source")
    if destination.suffix.lower() != ".csv":
        raise ValueError("export filename must end in .csv")
    if project_path is not None:
        _, _, preview = load_project(project_path)
        if preview is None:
            raise ValueError("project has no recorded data")
    descriptor, temp = _atomic_target(destination)
    try:
        with os.fdopen(descriptor, "wb") as target:
            if csv_path is not None:
                with csv_path.open("rb") as source:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
            else:
                with ZipFile(project_path) as archive, archive.open("capture.csv") as source:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        _commit(temp, destination)
    finally:
        temp.unlink(missing_ok=True)


def load_project(path: Path) -> tuple[Workspace, dict[str, object], RecordingPreview | None]:
    """Validate members and checksums before exposing data; never extract paths."""

    with ZipFile(path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or set(names) not in (
            {"manifest.json"}, {"manifest.json", "capture.csv", "capture.raw"}
        ):
            raise ValueError("project has unexpected or duplicate members")
        if any(info.file_size > MAX_RECORDING or info.compress_size > MAX_RECORDING
               for info in infos):
            raise ValueError("project member exceeds size limit")
        manifest_info = archive.getinfo("manifest.json")
        if manifest_info.file_size > MAX_MANIFEST:
            raise ValueError("project manifest is too large")
        data = json.loads(archive.read("manifest.json"))
        workspace, acquisition = _validated_manifest(data, project=True)
        recording = data.get("recording")
        if recording is None:
            if len(names) != 1:
                raise ValueError("unexpected recording members")
            return workspace, acquisition, None
        if not isinstance(recording, dict) or len(names) != 3:
            raise ValueError("incomplete recording")
        for member, prefix in (("capture.csv", "csv"), ("capture.raw", "raw")):
            expected_size = recording.get(f"{prefix}_bytes")
            expected_hash = recording.get(f"{prefix}_sha256")
            info = archive.getinfo(member)
            if info.file_size != expected_size or not isinstance(expected_hash, str):
                raise ValueError("recording size or checksum metadata is invalid")
            digest = hashlib.sha256()
            with archive.open(member) as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != expected_hash:
                raise ValueError(f"{member} checksum mismatch")
        # The GUI keeps a bounded viewport only; the original full data stays
        # inside the self-contained project archive.
        times: deque[float] = deque(maxlen=20000)
        values: deque[tuple[float, ...]] = deque(maxlen=20000)
        channels: tuple[int, ...] = ()
        with archive.open("capture.csv") as stream:
            header = stream.readline(4097)
            if len(header) > 4096 or not header.startswith(b"time_s,"):
                raise ValueError("invalid recording header")
            columns = header.decode("ascii").strip().split(",")[1:]
            try:
                channels = tuple(int(column.removeprefix("ai").removesuffix("_V"))
                                 for column in columns)
            except ValueError as exc:
                raise ValueError("invalid recording channels") from exc
            if not channels or len(set(channels)) != len(channels) or any(
                channel < 0 or channel > 7 or column != f"ai{channel}_V"
                for channel, column in zip(channels, columns, strict=True)
            ):
                raise ValueError("invalid recording channels")
            previous = -1.0
            for row in stream:
                if len(row) > 4096:
                    raise ValueError("recording row is too long")
                fields = row.decode("ascii").strip().split(",")
                if len(fields) != len(channels) + 1:
                    raise ValueError("recording row width mismatch")
                sample_time = float(fields[0])
                sample_volts = tuple(float(value) for value in fields[1:])
                if (not isfinite(sample_time) or sample_time < 0 or sample_time < previous
                        or any(not isfinite(value) for value in sample_volts)):
                    raise ValueError("recording contains invalid values or time ordering")
                times.append(sample_time)
                values.append(sample_volts)
                previous = sample_time
        return workspace, acquisition, RecordingPreview(channels, tuple(times), tuple(values))


def _validated_manifest(data: object, *, project: bool) -> tuple[Workspace, dict[str, object]]:
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ValueError("unsupported project schema")
    acquisition = data.get("acquisition")
    if not isinstance(acquisition, dict):
        raise TypeError("project acquisition settings are malformed")
    if not project and "recording" in data:
        raise ValueError("workspace preset cannot contain recorded data")
    return Workspace.from_dict(data.get("workspace")), acquisition
