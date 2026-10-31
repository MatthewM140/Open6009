"""Preallocated, ordered asynchronous libusb bulk-IN reader.

Only EP2 IN is exposed here.  A dedicated event thread keeps URBs pending
while the calling thread exchanges the captured EP1 control frames.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from threading import Condition, Thread
from time import monotonic

from ..errors import ProtocolError, UsbAccessError


class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class _Transfer(ctypes.Structure):
    _fields_ = [
        ("dev_handle", ctypes.c_void_p),
        ("flags", ctypes.c_uint8),
        ("endpoint", ctypes.c_uint8),
        ("type", ctypes.c_uint8),
        ("timeout", ctypes.c_uint),
        ("status", ctypes.c_int),
        ("length", ctypes.c_int),
        ("actual_length", ctypes.c_int),
        ("callback", ctypes.c_void_p),
        ("user_data", ctypes.c_void_p),
        ("buffer", ctypes.POINTER(ctypes.c_ubyte)),
        ("num_iso_packets", ctypes.c_int),
    ]


_TransferPtr = ctypes.POINTER(_Transfer)
_Callback = ctypes.CFUNCTYPE(None, _TransferPtr)


@dataclass(slots=True)
class _Slot:
    transfer: _TransferPtr
    buffer: ctypes.Array[ctypes.c_ubyte]
    sequence: int = -1


class AsyncBulkIn:
    """Keep EP2 reads in flight and return bytes in submission order."""

    def __init__(
        self, library: ctypes.CDLL, context: ctypes.c_void_p,
        handle: ctypes.c_void_p, *, transfers: int = 8,
        transfer_bytes: int = 2048, max_buffer_bytes: int = 1_048_576,
    ) -> None:
        if not 2 <= transfers <= 32 or transfer_bytes < 64 or transfer_bytes % 64:
            raise ValueError("invalid asynchronous EP2 transfer configuration")
        self._lib = library
        self._context = context
        self._handle = handle
        self._count = transfers
        self._size = transfer_bytes
        self._limit = max_buffer_bytes
        self._condition = Condition()
        self._callback = _Callback(self._on_complete)
        self._slots: dict[int, _Slot] = {}
        self._inflight: set[int] = set()
        self._completed: dict[int, bytes] = {}
        self._buffer = bytearray()
        self._queued_bytes = 0
        self._max_queued_bytes = 0
        self._completed_bytes = 0
        self._completed_transfers = 0
        self._zero_length_transfers = 0
        self._out_of_order_completions = 0
        self._next_completion = 0
        self._future_completions: set[int] = set()
        self._next_submit = 0
        self._next_take = 0
        self._stopping = False
        self._error: Exception | None = None
        self._thread = Thread(target=self._events, name="open6009-libusb-events", daemon=True)
        self._started = False
        self._configure()

    def _configure(self) -> None:
        lib = self._lib
        lib.libusb_alloc_transfer.argtypes = [ctypes.c_int]
        lib.libusb_alloc_transfer.restype = _TransferPtr
        lib.libusb_submit_transfer.argtypes = [_TransferPtr]
        lib.libusb_submit_transfer.restype = ctypes.c_int
        lib.libusb_cancel_transfer.argtypes = [_TransferPtr]
        lib.libusb_cancel_transfer.restype = ctypes.c_int
        lib.libusb_free_transfer.argtypes = [_TransferPtr]
        lib.libusb_free_transfer.restype = None
        lib.libusb_handle_events_timeout.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Timeval)]
        lib.libusb_handle_events_timeout.restype = ctypes.c_int

    def start(self) -> None:
        if self._started:
            raise RuntimeError("asynchronous EP2 reader already started")
        self._started = True
        self._thread.start()
        try:
            for _ in range(self._count):
                ptr = self._lib.libusb_alloc_transfer(0)
                if not ptr:
                    raise UsbAccessError("libusb could not allocate an EP2 transfer")
                buffer = (ctypes.c_ubyte * self._size)()
                item = ptr.contents
                item.dev_handle = self._handle
                item.flags = 0
                item.endpoint = 0x82
                item.type = 2  # LIBUSB_TRANSFER_TYPE_BULK
                item.timeout = 0  # Cancellation, not timeout, ends an in-flight read.
                item.length = self._size
                item.callback = ctypes.cast(self._callback, ctypes.c_void_p).value
                item.user_data = None
                item.buffer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
                item.num_iso_packets = 0
                key = ctypes.addressof(item)
                self._slots[key] = _Slot(ptr, buffer)
                with self._condition:
                    self._submit_locked(key)
        except Exception:
            self.stop()
            raise

    def _submit_locked(self, key: int) -> None:
        slot = self._slots[key]
        slot.sequence = self._next_submit
        result = self._lib.libusb_submit_transfer(slot.transfer)
        if result != 0:
            raise UsbAccessError(f"async EP2 submit failed: {result}")
        self._next_submit += 1
        self._inflight.add(key)

    def _on_complete(self, ptr: _TransferPtr) -> None:
        # Never let a Python exception escape through a C callback.
        try:
            key = ctypes.addressof(ptr.contents)
            with self._condition:
                slot = self._slots[key]
                self._inflight.discard(key)
                status = ptr.contents.status
                if status == 0:
                    if not 0 <= ptr.contents.actual_length <= self._size:
                        raise ProtocolError("invalid asynchronous EP2 completion length")
                    data = bytes(slot.buffer[:ptr.contents.actual_length])
                    if slot.sequence != self._next_completion:
                        self._out_of_order_completions += 1
                        self._future_completions.add(slot.sequence)
                    else:
                        self._next_completion += 1
                        while self._next_completion in self._future_completions:
                            self._future_completions.remove(self._next_completion)
                            self._next_completion += 1
                    self._completed[slot.sequence] = data
                    self._queued_bytes += len(data)
                    self._completed_bytes += len(data)
                    self._completed_transfers += 1
                    self._zero_length_transfers += not data
                    self._max_queued_bytes = max(self._max_queued_bytes, self._queued_bytes)
                    if self._queued_bytes > self._limit:
                        self._error = ProtocolError("host EP2 queue exceeded its bounded capacity")
                elif not (self._stopping and status == 3):
                    self._error = UsbAccessError(f"asynchronous EP2 transfer status {status}")
                if not self._stopping and self._error is None:
                    self._submit_locked(key)
                self._condition.notify_all()
        except Exception as exc:  # noqa: BLE001 - exceptions must not cross the C callback
            with self._condition:
                self._error = exc
                self._condition.notify_all()

    def _events(self) -> None:
        timeout = _Timeval(0, 50_000)
        while True:
            with self._condition:
                if self._stopping and not self._inflight:
                    return
            result = self._lib.libusb_handle_events_timeout(self._context, ctypes.byref(timeout))
            if result == -10:  # LIBUSB_ERROR_INTERRUPTED
                continue
            if result < 0:
                with self._condition:
                    self._error = UsbAccessError(f"libusb event handling failed: {result}")
                    self._condition.notify_all()
                return

    def take(self, size: int, *, deadline: float) -> bytes:
        with self._condition:
            while len(self._buffer) < size:
                while self._next_take in self._completed:
                    self._buffer.extend(self._completed.pop(self._next_take))
                    self._next_take += 1
                if self._error is not None:
                    raise self._error
                if len(self._buffer) >= size:
                    break
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise ProtocolError(
                        f"asynchronous EP2 timeout with {len(self._buffer)} of {size} bytes"
                    )
                self._condition.wait(remaining)
            data = bytes(self._buffer[:size])
            del self._buffer[:size]
            self._queued_bytes -= size
            return data

    def stop(self) -> None:
        if not self._started:
            return
        with self._condition:
            self._stopping = True
            pending = [self._slots[key].transfer for key in self._inflight]
            self._condition.notify_all()
        for ptr in pending:
            self._lib.libusb_cancel_transfer(ptr)
        self._thread.join(timeout=3.0)
        if self._thread.is_alive():
            raise UsbAccessError("asynchronous EP2 cancellation did not complete")
        # If the event loop itself failed, cancellation callbacks may still
        # need to be delivered before transfer buffers can be freed.
        deadline = monotonic() + 3.0
        while self._inflight and monotonic() < deadline:
            timeout = _Timeval(0, 50_000)
            self._lib.libusb_handle_events_timeout(self._context, ctypes.byref(timeout))
        if self._inflight:
            raise UsbAccessError("asynchronous EP2 transfers remain in flight")
        for slot in self._slots.values():
            self._lib.libusb_free_transfer(slot.transfer)
        self._slots.clear()
        self._completed.clear()
        self._buffer.clear()
        self._queued_bytes = 0
        self._started = False
        if self._error is not None:
            raise self._error

    @property
    def buffered_bytes(self) -> int:
        with self._condition:
            return self._queued_bytes

    @property
    def statistics(self) -> dict[str, int]:
        with self._condition:
            return {
                "completed_bytes": self._completed_bytes,
                "completed_transfers": self._completed_transfers,
                "zero_length_transfers": self._zero_length_transfers,
                "out_of_order_completions": self._out_of_order_completions,
                "max_queued_bytes": self._max_queued_bytes,
            }

    @property
    def active(self) -> bool:
        """Whether in-flight transfers might still own the USB handle."""

        return self._started
