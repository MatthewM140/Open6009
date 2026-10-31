"""Minimal ctypes binding to system libusb-1.0.

The binding deliberately omits reset, set-configuration, kernel-detach, and
firmware-related APIs. Its only control transfer is the firmware-confirmed,
standard USB GET_CONFIGURATION read; generic control transfers are not exposed.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from dataclasses import dataclass
from typing import Self

from ..constants import PRODUCT_ID, VENDOR_ID
from ..errors import DeviceNotFoundError, UsbAccessError
from .async_bulk import AsyncBulkIn


class _DeviceDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("bcdUSB", ctypes.c_uint16),
        ("bDeviceClass", ctypes.c_uint8),
        ("bDeviceSubClass", ctypes.c_uint8),
        ("bDeviceProtocol", ctypes.c_uint8),
        ("bMaxPacketSize0", ctypes.c_uint8),
        ("idVendor", ctypes.c_uint16),
        ("idProduct", ctypes.c_uint16),
        ("bcdDevice", ctypes.c_uint16),
        ("iManufacturer", ctypes.c_uint8),
        ("iProduct", ctypes.c_uint8),
        ("iSerialNumber", ctypes.c_uint8),
        ("bNumConfigurations", ctypes.c_uint8),
    ]


@dataclass(frozen=True)
class LibusbDeviceIdentity:
    bus: int
    address: int
    vendor_id: int
    product_id: int
    device_version_bcd: int


class Libusb:
    def __init__(self) -> None:
        path = ctypes.util.find_library("usb-1.0")
        if path is None:
            raise UsbAccessError("system libusb-1.0 shared library was not found")
        self.library = ctypes.CDLL(path)
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        lib = self.library
        lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        lib.libusb_init.restype = ctypes.c_int
        lib.libusb_exit.argtypes = [ctypes.c_void_p]
        lib.libusb_get_device_list.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ]
        lib.libusb_get_device_list.restype = ctypes.c_ssize_t
        lib.libusb_free_device_list.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
        lib.libusb_get_device_descriptor.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_DeviceDescriptor),
        ]
        lib.libusb_get_device_descriptor.restype = ctypes.c_int
        lib.libusb_get_bus_number.argtypes = [ctypes.c_void_p]
        lib.libusb_get_bus_number.restype = ctypes.c_uint8
        lib.libusb_get_device_address.argtypes = [ctypes.c_void_p]
        lib.libusb_get_device_address.restype = ctypes.c_uint8
        lib.libusb_open.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        lib.libusb_open.restype = ctypes.c_int
        lib.libusb_close.argtypes = [ctypes.c_void_p]
        lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.libusb_claim_interface.restype = ctypes.c_int
        lib.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.libusb_release_interface.restype = ctypes.c_int
        lib.libusb_bulk_transfer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ubyte,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_uint,
        ]
        lib.libusb_bulk_transfer.restype = ctypes.c_int
        lib.libusb_control_transfer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint8,
            ctypes.c_uint8,
            ctypes.c_uint16,
            ctypes.c_uint16,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_uint16,
            ctypes.c_uint,
        ]
        lib.libusb_control_transfer.restype = ctypes.c_int
        lib.libusb_error_name.argtypes = [ctypes.c_int]
        lib.libusb_error_name.restype = ctypes.c_char_p

    def error_name(self, code: int) -> str:
        value = self.library.libusb_error_name(code)
        return value.decode("ascii", errors="replace") if value else f"libusb error {code}"

    def initialize(self) -> ctypes.c_void_p:
        context = ctypes.c_void_p()
        result = self.library.libusb_init(ctypes.byref(context))
        if result != 0:
            raise UsbAccessError(f"libusb initialization failed: {result} ({self.error_name(result)})")
        return context

    def list_identities(self) -> list[LibusbDeviceIdentity]:
        context = self.initialize()
        devices = ctypes.POINTER(ctypes.c_void_p)()
        try:
            count = self.library.libusb_get_device_list(context, ctypes.byref(devices))
            if count < 0:
                raise UsbAccessError(
                    f"libusb device listing failed: {count} ({self.error_name(int(count))})"
                )
            identities: list[LibusbDeviceIdentity] = []
            for index in range(count):
                device = devices[index]
                descriptor = _DeviceDescriptor()
                result = self.library.libusb_get_device_descriptor(device, ctypes.byref(descriptor))
                if result != 0:
                    continue
                identities.append(
                    LibusbDeviceIdentity(
                        bus=int(self.library.libusb_get_bus_number(device)),
                        address=int(self.library.libusb_get_device_address(device)),
                        vendor_id=descriptor.idVendor,
                        product_id=descriptor.idProduct,
                        device_version_bcd=descriptor.bcdDevice,
                    )
                )
            return identities
        finally:
            if bool(devices):
                self.library.libusb_free_device_list(devices, 1)
            self.library.libusb_exit(context)

    def get_configuration(self, handle: ctypes.c_void_p, timeout_ms: int = 1000) -> int:
        """Perform only standard USB GET_CONFIGURATION (``80 08 ... 01 00``).

        This is intentionally not a generic control-transfer API. The USB-6009
        firmware's endpoint-zero dispatcher and response length establish this
        read-only request independently of any proprietary protocol.
        """

        if not 1 <= timeout_ms <= 5000:
            raise ValueError("timeout_ms must be between 1 and 5000")
        response = (ctypes.c_ubyte * 1)()
        result = self.library.libusb_control_transfer(
            handle,
            0x80,  # standard, device recipient, IN
            0x08,  # GET_CONFIGURATION
            0,
            0,
            response,
            1,
            timeout_ms,
        )
        if result < 0:
            raise UsbAccessError(
                f"GET_CONFIGURATION failed: {result} ({self.error_name(result)})"
            )
        if result != 1:
            raise UsbAccessError(f"GET_CONFIGURATION returned {result} bytes, expected 1")
        return int(response[0])

    def get_status(self, handle: ctypes.c_void_p, timeout_ms: int = 1000) -> int:
        """Perform only standard USB device GET_STATUS (``80 00 ... 02 00``)."""

        if not 1 <= timeout_ms <= 5000:
            raise ValueError("timeout_ms must be between 1 and 5000")
        response = (ctypes.c_ubyte * 2)()
        result = self.library.libusb_control_transfer(
            handle,
            0x80,  # standard, device recipient, IN
            0x00,  # GET_STATUS
            0,
            0,
            response,
            2,
            timeout_ms,
        )
        if result < 0:
            raise UsbAccessError(f"GET_STATUS failed: {result} ({self.error_name(result)})")
        if result != 2:
            raise UsbAccessError(f"GET_STATUS returned {result} bytes, expected 2")
        return int.from_bytes(bytes(response), "little")

    def read_device_configuration(self, timeout_ms: int = 1000) -> int:
        """Open the USB-6009, read its standard configuration byte, then close.

        This method does not claim an interface and never sends a vendor or bulk
        transfer. It is suitable only for the explicit opt-in CLI probe.
        """

        context = self.initialize()
        devices = ctypes.POINTER(ctypes.c_void_p)()
        handle = ctypes.c_void_p()
        try:
            count = self.library.libusb_get_device_list(context, ctypes.byref(devices))
            if count < 0:
                raise UsbAccessError(
                    f"libusb device listing failed: {count} ({self.error_name(int(count))})"
                )
            for index in range(count):
                descriptor = _DeviceDescriptor()
                device = devices[index]
                if self.library.libusb_get_device_descriptor(device, ctypes.byref(descriptor)) != 0:
                    continue
                if descriptor.idVendor != VENDOR_ID or descriptor.idProduct != PRODUCT_ID:
                    continue
                result = self.library.libusb_open(device, ctypes.byref(handle))
                if result != 0:
                    raise UsbAccessError(
                        f"cannot open 3923:717b: {result} ({self.error_name(result)})"
                    )
                return self.get_configuration(handle, timeout_ms)
            raise DeviceNotFoundError("NI USB-6009 not found by libusb")
        finally:
            if bool(handle):
                self.library.libusb_close(handle)
            if bool(devices):
                self.library.libusb_free_device_list(devices, 1)
            self.library.libusb_exit(context)

    def read_device_status(self, timeout_ms: int = 1000) -> int:
        """Open the USB-6009, read its standard status bitmap, then close.

        Like ``read_device_configuration``, this does not claim an interface
        and does not expose vendor or bulk transfers.
        """

        context = self.initialize()
        devices = ctypes.POINTER(ctypes.c_void_p)()
        handle = ctypes.c_void_p()
        try:
            count = self.library.libusb_get_device_list(context, ctypes.byref(devices))
            if count < 0:
                raise UsbAccessError(
                    f"libusb device listing failed: {count} ({self.error_name(int(count))})"
                )
            for index in range(count):
                descriptor = _DeviceDescriptor()
                device = devices[index]
                if self.library.libusb_get_device_descriptor(device, ctypes.byref(descriptor)) != 0:
                    continue
                if descriptor.idVendor != VENDOR_ID or descriptor.idProduct != PRODUCT_ID:
                    continue
                result = self.library.libusb_open(device, ctypes.byref(handle))
                if result != 0:
                    raise UsbAccessError(
                        f"cannot open 3923:717b: {result} ({self.error_name(result)})"
                    )
                return self.get_status(handle, timeout_ms)
            raise DeviceNotFoundError("NI USB-6009 not found by libusb")
        finally:
            if bool(handle):
                self.library.libusb_close(handle)
            if bool(devices):
                self.library.libusb_free_device_list(devices, 1)
            self.library.libusb_exit(context)


class LibusbTransport:
    def __init__(self, api: Libusb, context: ctypes.c_void_p, handle: ctypes.c_void_p):
        self._api = api
        self._context = context
        self._handle = handle
        self._claimed = True
        self._async_readers: list[AsyncBulkIn] = []

    def open_async_ep2(self, *, transfers: int = 8, transfer_bytes: int = 2048) -> AsyncBulkIn:
        """Queue preallocated EP2 IN transfers on this claimed device."""

        reader = AsyncBulkIn(
            self._api.library, self._context, self._handle,
            transfers=transfers, transfer_bytes=transfer_bytes,
        )
        reader.start()
        self._async_readers.append(reader)
        return reader

    def close_async_ep2(self, reader: AsyncBulkIn) -> None:
        try:
            reader.stop()
        finally:
            if not reader.active:
                self._async_readers.remove(reader)

    @classmethod
    def open(cls) -> Self:
        api = Libusb()
        context = api.initialize()
        devices = ctypes.POINTER(ctypes.c_void_p)()
        handle = ctypes.c_void_p()
        try:
            count = api.library.libusb_get_device_list(context, ctypes.byref(devices))
            if count < 0:
                raise UsbAccessError(api.error_name(int(count)))
            found = False
            for index in range(count):
                descriptor = _DeviceDescriptor()
                device = devices[index]
                if api.library.libusb_get_device_descriptor(device, ctypes.byref(descriptor)) != 0:
                    continue
                if descriptor.idVendor == VENDOR_ID and descriptor.idProduct == PRODUCT_ID:
                    found = True
                    result = api.library.libusb_open(device, ctypes.byref(handle))
                    if result != 0:
                        raise UsbAccessError(
                            f"cannot open 3923:717b: {result} ({api.error_name(result)})"
                        )
                    break
            if not found:
                raise DeviceNotFoundError("NI USB-6009 not found by libusb")
            result = api.library.libusb_claim_interface(handle, 0)
            if result != 0:
                api.library.libusb_close(handle)
                handle = ctypes.c_void_p()
                raise UsbAccessError(
                    f"cannot claim interface 0: {result} ({api.error_name(result)})"
                )
            return cls(api, context, handle)
        except Exception:
            api.library.libusb_exit(context)
            raise
        finally:
            if bool(devices):
                api.library.libusb_free_device_list(devices, 1)

    def write(self, endpoint: int, data: bytes, timeout_ms: int = 1000) -> int:
        if endpoint & 0x80:
            raise ValueError("write endpoint must be OUT")
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        transferred = ctypes.c_int()
        result = self._api.library.libusb_bulk_transfer(
            self._handle, endpoint, buffer, len(data), ctypes.byref(transferred), timeout_ms
        )
        if result != 0:
            raise UsbAccessError(f"bulk write failed: {result} ({self._api.error_name(result)})")
        return transferred.value

    def read(self, endpoint: int, size: int, timeout_ms: int = 1000) -> bytes:
        if not endpoint & 0x80:
            raise ValueError("read endpoint must be IN")
        buffer = (ctypes.c_ubyte * size)()
        transferred = ctypes.c_int()
        result = self._api.library.libusb_bulk_transfer(
            self._handle, endpoint, buffer, size, ctypes.byref(transferred), timeout_ms
        )
        if result != 0:
            raise UsbAccessError(f"bulk read failed: {result} ({self._api.error_name(result)})")
        return bytes(buffer[: transferred.value])

    def close(self) -> None:
        reader_error: Exception | None = None
        for reader in self._async_readers:
            try:
                reader.stop()
            except Exception as exc:
                if reader.active:
                    raise
                if reader_error is None:
                    reader_error = exc
        self._async_readers.clear()
        release_error: tuple[int, str] | None = None
        if self._claimed:
            result = self._api.library.libusb_release_interface(self._handle, 0)
            if result != 0:
                release_error = (result, self._api.error_name(result))
            self._claimed = False
        if bool(self._handle):
            self._api.library.libusb_close(self._handle)
            self._handle = ctypes.c_void_p()
        if bool(self._context):
            self._api.library.libusb_exit(self._context)
            self._context = ctypes.c_void_p()
        if release_error is not None:
            code, name = release_error
            raise UsbAccessError(f"cannot release interface 0: {code} ({name})")
        if reader_error is not None:
            raise reader_error

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            self.close()
        except UsbAccessError:
            # Preserve an exception raised by the protected operation. A release
            # failure is still raised normally when leaving a successful block.
            if exc_type is None:
                raise
