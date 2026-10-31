"""Environment diagnostics with no vendor-specific USB transfers."""

from __future__ import annotations

import importlib.util
import os
import platform
from dataclasses import asdict, dataclass
from pathlib import Path

from .descriptors import discover_sysfs, parse_descriptors
from .errors import Open6009Error


@dataclass(frozen=True)
class DiagnosticReport:
    kernel: str
    uid: int
    pyusb_installed: bool
    libusb_status: str
    sysfs_devices: tuple[dict[str, object], ...]


def collect_diagnostics() -> DiagnosticReport:
    records: list[dict[str, object]] = []
    for device in discover_sysfs():
        record: dict[str, object] = {
            "sysfs_path": str(device.path),
            "usbfs_path": str(device.usbfs_path),
            "usbfs_exists": device.usbfs_path.exists(),
            "usbfs_readable": os.access(device.usbfs_path, os.R_OK),
            "usbfs_writable": os.access(device.usbfs_path, os.W_OK),
            "bus": device.bus_number,
            "address": device.device_number,
            "vid": f"{device.vendor_id:04x}",
            "pid": f"{device.product_id:04x}",
            "serial": device.serial,
            "product": device.product,
            "speed_mbps": device.speed_mbps,
            "device_version": device.device_version,
        }
        try:
            parsed = parse_descriptors(device.read_descriptors())
            record["endpoints"] = [
                f"0x{ep.address:02x}:{ep.transfer_type}:{ep.max_packet_size}"
                for interface in parsed.interfaces
                for ep in interface.endpoints
            ]
        except (OSError, ValueError) as exc:
            record["descriptor_error"] = str(exc)
        records.append(record)
    try:
        from .transport.libusb1 import Libusb

        identities = Libusb().list_identities()
        libusb_status = f"ok: enumerated {len(identities)} USB devices"
    except (Open6009Error, OSError) as exc:
        libusb_status = f"unavailable: {exc}"
    return DiagnosticReport(
        kernel=platform.release(),
        uid=os.getuid(),
        pyusb_installed=importlib.util.find_spec("usb") is not None,
        libusb_status=libusb_status,
        sysfs_devices=tuple(records),
    )


def report_as_dict(report: DiagnosticReport) -> dict[str, object]:
    return asdict(report)


def explain_access(record: dict[str, object]) -> str:
    path = Path(str(record["usbfs_path"]))
    if not path.parent.parent.exists():
        return "/dev/bus/usb is absent; libusb cannot enumerate this namespace"
    if not path.exists():
        return f"expected usbfs node {path} is absent"
    if not record["usbfs_readable"] or not record["usbfs_writable"]:
        return f"usbfs node exists but uid {os.getuid()} lacks read/write access"
    return "usbfs node is available for libusb"
