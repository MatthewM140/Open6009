"""Single owner of the USB handle; no device operation runs on the Qt thread."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from queue import Empty, Queue
from statistics import fmean
from threading import Event
from time import monotonic

from PySide6.QtCore import QThread, Signal

from open6009.acquisition.config import AnalogInputConfig
from open6009.constants import PRODUCT_ID, VENDOR_ID
from open6009.device import Usb6009
from open6009.transport.libusb1 import Libusb, LibusbTransport

from .model import AcquisitionSettings, PlotBatch
from .recording import CsvRecorder


def discover_device() -> str:
    """Identify one connected USB-6009 without claiming its interface."""

    matches = [item for item in Libusb().list_identities()
               if (item.vendor_id, item.product_id) == (VENDOR_ID, PRODUCT_ID)]
    if not matches:
        raise RuntimeError("NI USB-6009 is not connected")
    if len(matches) != 1:
        raise RuntimeError("multiple USB-6009 devices found; disconnect extras before connecting")
    item = matches[0]
    serial = None
    for candidate in Path("/sys/bus/usb/devices").glob("*/serial"):
        try:
            parent = candidate.parent
            if (int((parent / "busnum").read_text()) == item.bus
                    and int((parent / "devnum").read_text()) == item.address):
                serial = candidate.read_text().strip()
                break
        except (OSError, ValueError):
            continue
    serial_label = serial or "serial unavailable"
    return f"NI USB-6009 · {serial_label} · bus {item.bus:03d}/{item.address:03d}"


class DeviceWorker(QThread):
    state_changed = Signal(str, str)
    batch_ready = Signal(object)
    result_ready = Signal(str, object)
    error_occurred = Signal(str)

    def __init__(self, *, opener: Callable[[], object] = LibusbTransport.open,
                 discover: Callable[[], str] = discover_device) -> None:
        super().__init__()
        self._opener = opener
        self._discover = discover
        self._commands: Queue[tuple[str, object | None]] = Queue()
        self._stop = Event()
        self._device: Usb6009 | None = None
        self._shutdown = False

    def submit(self, operation: str, payload: object | None = None) -> None:
        if operation == "acquire":
            self._stop.clear()
        if operation in {"stop", "disconnect", "shutdown"}:
            self._stop.set()
        self._commands.put((operation, payload))

    def _emit_state(self, state: str, detail: str = "") -> None:
        self.state_changed.emit(state, detail)

    def _connected_device(self) -> Usb6009:
        if self._device is None:
            raise RuntimeError("connect the USB-6009 before using hardware controls")
        return self._device

    def _publish_batch(self, batch: object, settings: AcquisitionSettings,
                       scans_before: int, started: float) -> None:
        stride = max(1, int(settings.rate_per_channel * settings.display_seconds / 3000))
        indices = range(0, len(batch.scans_volts), stride)
        first_channel = [values[0] for values in batch.scans_volts]
        self.batch_ready.emit(PlotBatch(
            settings.channels,
            tuple((scans_before + index) / settings.rate_per_channel for index in indices),
            tuple(batch.scans_volts[index] for index in indices),
            scans_before + batch.samples_per_channel,
            (scans_before + batch.samples_per_channel) * 2 * len(settings.channels),
            monotonic() - started,
            (min(first_channel), fmean(first_channel), max(first_channel)),
        ))

    def _read_combined_digital_ports(self, device: Usb6009,
                                     settings: AcquisitionSettings) -> None:
        """Serialize input-only EP1 reads while asynchronous EP2 AI remains armed."""

        for port in settings.digital_ports:
            value = device.read_captured_digital_port(port, explicitly_authorized=True)
            self.result_ready.emit(
                "digital_poll", (port, value, settings.digital_generation))

    def _acquire(self, settings: AcquisitionSettings) -> None:
        settings.validate()
        device = self._connected_device()
        self._emit_state("Acquiring", "Calibrated analog input")
        recorder = (CsvRecorder(Path(settings.record_path), settings.channels,
                                settings.rate_per_channel, settings.mode)
                    if settings.record_path else None)
        started = monotonic()
        completed = False
        acquisition_started = False
        calibration_replies: tuple[str, ...] = ()
        problem: Exception | None = None
        try:
            if settings.finite_scans is not None:
                config = AnalogInputConfig(settings.channels, settings.mode,
                                           settings.voltage_range, settings.finite_scans,
                                           settings.rate_per_channel)
                batch = device.acquire_analog_volts(config, explicitly_authorized=True)
                calibration_replies = tuple(item.raw_reply.hex() for item in batch.calibrations)
                if recorder is not None:
                    recorder.append(batch)
                self._publish_batch(batch, settings, 0, started)
                completed = True
            else:
                device.start_continuous_ai(channels=settings.channels,
                                           sample_rate_hz=settings.rate_per_channel,
                                           explicitly_authorized=True)
                acquisition_started = True
                scans = 0
                next_digital_poll = monotonic()
                digital_interval = 1 / settings.digital_poll_hz
                while not self._stop.is_set():
                    now = monotonic()
                    if settings.digital_ports and now >= next_digital_poll:
                        self._read_combined_digital_ports(device, settings)
                        # Do not burst to catch up after a long analog chunk.
                        next_digital_poll = monotonic() + digital_interval
                    batch = device.read_continuous_ai_volts()
                    if not calibration_replies:
                        calibration_replies = tuple(item.raw_reply.hex()
                                                    for item in batch.calibrations)
                    if recorder is not None:
                        recorder.append(batch)
                    self._publish_batch(batch, settings, scans, started)
                    scans += batch.samples_per_channel
                completed = True
        except Exception as exc:  # noqa: BLE001 - USB operation must always reach cleanup
            problem = exc
        finally:
            self._emit_state("Stopping", "Releasing acquisition")
            if acquisition_started:
                try:
                    device.stop_continuous_ai()
                except Exception as exc:  # noqa: BLE001 - preserve primary acquisition failure
                    problem = problem or exc
            if recorder is not None:
                try:
                    recorder.finish(success=completed and problem is None)
                except Exception as exc:  # noqa: BLE001 - report writer failure after USB cleanup
                    problem = problem or exc
            self._emit_state("Connected", "Ready")
            if problem is not None:
                raise problem
            self.result_ready.emit("acquisition_finished", {
                "recording": settings.record_path if recorder is not None else None,
                "elapsed_seconds": monotonic() - started,
                "calibration_replies": calibration_replies,
                "async_transfer_statistics": (device.last_continuous_async_statistics
                                               if acquisition_started else {}),
            })

    def _execute(self, name: str, payload: object | None) -> None:
        if name == "probe":
            try:
                identity = self._discover()
            except (OSError, RuntimeError):
                identity = None
            self.result_ready.emit("probe", identity)
        elif name == "connect":
            if self._device is not None:
                return
            identity = self._discover()
            self._emit_state("Connecting", identity)
            self._device = Usb6009(self._opener())
            self._emit_state("Connected", identity)
            self.result_ready.emit("connected", identity)
        elif name in {"disconnect", "shutdown"}:
            if name == "shutdown":
                self._shutdown = True
            if self._device is not None:
                device = self._device
                self._device = None
                device.close()
            self._emit_state("Disconnected", "USB interface released")
            self.result_ready.emit("disconnected", None)
        elif name == "acquire":
            if not isinstance(payload, AcquisitionSettings):
                raise ValueError("invalid acquisition settings")
            self._acquire(payload)
        elif name == "digital_read":
            port = int(payload)
            value = self._connected_device().read_captured_digital_port(
                port, explicitly_authorized=True)
            self.result_ready.emit("digital_read", (port, value))
        elif name == "digital_poll":
            port, generation = payload
            value = self._connected_device().read_captured_digital_port(
                int(port), explicitly_authorized=True)
            self.result_ready.emit("digital_poll", (int(port), value, int(generation)))
        elif name == "digital_write":
            port, value = payload
            self._connected_device().write_digital_port(port, value,
                                                        explicitly_authorized=True)
            self.result_ready.emit("digital_write", (port, value))
        elif name == "ao_write":
            channel, voltage = payload
            code = self._connected_device().set_reference_ao_voltage(
                channel, voltage, explicitly_authorized=True)
            self.result_ready.emit("ao_write", (channel, voltage, code))
        elif name == "counter_start":
            self._connected_device().start_counter(explicitly_authorized=True)
            self.result_ready.emit("counter", "started")
        elif name == "counter_read":
            raw, value = self._connected_device().read_counter_raw()
            self.result_ready.emit("counter_read", (raw.hex(), value))
        elif name == "counter_stop":
            self._connected_device().stop_counter()
            self.result_ready.emit("counter", "stopped")
        elif name == "stop":
            return
        else:
            raise ValueError(f"unknown worker operation: {name}")

    def run(self) -> None:
        self._emit_state("Disconnected", "Waiting for device")
        while not self._shutdown:
            try:
                name, payload = self._commands.get(timeout=0.1)
            except Empty:
                continue
            try:
                self._execute(name, payload)
            except Exception as exc:  # noqa: BLE001 - keep worker alive after hardware failure
                self._emit_state("Connected" if self._device else "Disconnected", "Operation failed")
                self.error_occurred.emit(str(exc))
        if self._device is not None:
            try:
                self._device.close()
            except Exception as exc:  # noqa: BLE001 - report final cleanup failure
                self.error_occurred.emit(str(exc))
