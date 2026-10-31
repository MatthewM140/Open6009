"""Read-only Linux sysfs discovery and USB descriptor parsing."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .constants import PRODUCT_ID, VENDOR_ID
from .errors import ProtocolError


@dataclass(frozen=True)
class EndpointDescriptor:
    address: int
    attributes: int
    max_packet_size: int
    interval: int

    @property
    def direction(self) -> str:
        return "in" if self.address & 0x80 else "out"

    @property
    def transfer_type(self) -> str:
        return ("control", "isochronous", "bulk", "interrupt")[self.attributes & 0x03]


@dataclass(frozen=True)
class InterfaceDescriptor:
    number: int
    alternate_setting: int
    interface_class: int
    interface_subclass: int
    interface_protocol: int
    endpoints: tuple[EndpointDescriptor, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DeviceDescriptorSet:
    usb_version_bcd: int
    device_version_bcd: int
    vendor_id: int
    product_id: int
    max_packet_size0: int
    configurations: int
    interfaces: tuple[InterfaceDescriptor, ...]


@dataclass(frozen=True)
class SysfsDevice:
    path: Path
    vendor_id: int
    product_id: int
    bus_number: int
    device_number: int
    speed_mbps: str
    manufacturer: str | None
    product: str | None
    serial: str | None
    device_version: str
    configuration: int

    @property
    def usbfs_path(self) -> Path:
        return Path(f"/dev/bus/usb/{self.bus_number:03d}/{self.device_number:03d}")

    @property
    def descriptors_path(self) -> Path:
        return self.path / "descriptors"

    def read_descriptors(self) -> bytes:
        return self.descriptors_path.read_bytes()


def _text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except (FileNotFoundError, PermissionError):
        return None


def discover_sysfs(
    root: Path = Path("/sys/bus/usb/devices"),
    vendor_id: int = VENDOR_ID,
    product_id: int = PRODUCT_ID,
) -> Iterator[SysfsDevice]:
    """Yield matching devices without opening or changing USB state."""
    if not root.exists():
        return
    for path in sorted(root.iterdir()):
        vendor = _text(path / "idVendor")
        product = _text(path / "idProduct")
        if vendor is None or product is None:
            continue
        if int(vendor, 16) != vendor_id or int(product, 16) != product_id:
            continue
        yield SysfsDevice(
            path=path,
            vendor_id=int(vendor, 16),
            product_id=int(product, 16),
            bus_number=int(_text(path / "busnum") or "0"),
            device_number=int(_text(path / "devnum") or "0"),
            speed_mbps=_text(path / "speed") or "unknown",
            manufacturer=_text(path / "manufacturer"),
            product=_text(path / "product"),
            serial=_text(path / "serial"),
            device_version=_text(path / "bcdDevice") or "unknown",
            configuration=int(_text(path / "bConfigurationValue") or "0"),
        )


def parse_descriptors(data: bytes) -> DeviceDescriptorSet:
    """Parse the device/config/interface/endpoint fields used by this project."""
    if len(data) < 18 or data[0] != 18 or data[1] != 1:
        raise ProtocolError("missing or malformed USB device descriptor")
    interfaces: list[InterfaceDescriptor] = []
    current: dict[str, object] | None = None
    offset = 18
    while offset < len(data):
        length = data[offset]
        if length < 2 or offset + length > len(data):
            raise ProtocolError(f"malformed descriptor at byte {offset}")
        dtype = data[offset + 1]
        chunk = data[offset : offset + length]
        if dtype == 4 and length >= 9:
            if current is not None:
                interfaces.append(_finish_interface(current))
            current = {
                "number": chunk[2],
                "alternate": chunk[3],
                "class": chunk[5],
                "subclass": chunk[6],
                "protocol": chunk[7],
                "endpoints": [],
            }
        elif dtype == 5 and length >= 7 and current is not None:
            endpoints = current["endpoints"]
            assert isinstance(endpoints, list)
            endpoints.append(
                EndpointDescriptor(
                    address=chunk[2],
                    attributes=chunk[3],
                    max_packet_size=int.from_bytes(chunk[4:6], "little"),
                    interval=chunk[6],
                )
            )
        offset += length
    if current is not None:
        interfaces.append(_finish_interface(current))
    return DeviceDescriptorSet(
        usb_version_bcd=int.from_bytes(data[2:4], "little"),
        device_version_bcd=int.from_bytes(data[12:14], "little"),
        vendor_id=int.from_bytes(data[8:10], "little"),
        product_id=int.from_bytes(data[10:12], "little"),
        max_packet_size0=data[7],
        configurations=data[17],
        interfaces=tuple(interfaces),
    )


def _finish_interface(values: dict[str, object]) -> InterfaceDescriptor:
    endpoints = values["endpoints"]
    assert isinstance(endpoints, list)
    return InterfaceDescriptor(
        number=int(values["number"]),
        alternate_setting=int(values["alternate"]),
        interface_class=int(values["class"]),
        interface_subclass=int(values["subclass"]),
        interface_protocol=int(values["protocol"]),
        endpoints=tuple(endpoints),
    )
