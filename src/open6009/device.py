"""High-level USB-6009 API with unsafe operations failing closed."""

from __future__ import annotations

from math import isfinite
from time import monotonic
from typing import Self

from .acquisition.calibration import (
    CalibratedAiBatch,
    decode_ai_calibration_reply,
    decode_calibrated_scans,
)
from .acquisition.config import AnalogInputConfig, TerminalMode
from .errors import (
    AcquisitionOverflowError,
    EvidenceRequiredError,
    Open6009Error,
    ProtocolError,
    UsbAccessError,
)
from .protocol import reference_counter as counter
from .protocol.digital_input import captured_digital_input_port_plan, decode_captured_port_reply
from .protocol.reference_ai import (
    _GENERIC_REPLY,
    DIFFERENTIAL_CALIBRATION_REPLY_PREFIX,
    captured_ai0_rse_finite_plan,
    captured_differential_finite_plan,
)
from .protocol.reference_ai_continuous import ContinuousAiPlan, captured_continuous_rse_plan
from .protocol.reference_ai_multi import captured_rse_multichannel_plan
from .protocol.reference_output import (
    CapturedOutputPlan,
    ao_voltage_to_code,
    captured_ao_code_plan,
    captured_ao_zero_plan,
    captured_digital_port_plan,
    decode_ao_calibration_replies,
)
from .transport.async_bulk import AsyncBulkIn
from .transport.base import BulkTransport
from .transport.libusb1 import LibusbTransport


class Usb6009:
    def __init__(self, transport: BulkTransport):
        self.transport = transport
        self._digital_output_shadow: dict[int, int] = {}
        self._counter_active = False
        self._continuous_ai_plan: ContinuousAiPlan | None = None
        self._continuous_ai_total_scans = 0
        self._continuous_ai_buffer = bytearray()
        self._continuous_async_ep2: AsyncBulkIn | None = None
        self._last_continuous_async_stats: dict[str, int] = {}
        self._last_continuous_drain_bytes = 0
        self._continuous_calibration_replies: tuple[bytes, ...] = ()
        self._last_ai_calibration_replies: list[bytes] = []

    @classmethod
    def open(cls) -> Self:
        """Open and claim the first connected NI USB-6009."""

        return cls(LibusbTransport.open())

    def acquire_analog(
        self, config: AnalogInputConfig, *, explicitly_authorized: bool = False
    ) -> bytes:
        """Acquire raw finite data for captured-supported configurations.

        Multichannel finite acquisition is RSE only. Continuous acquisition
        and NI-equivalent voltage conversion use separate public methods.
        """

        if not explicitly_authorized:
            raise EvidenceRequiredError("native analog acquisition requires explicit authorization")
        if self._continuous_ai_plan is not None or self._counter_active:
            raise Open6009Error("another streaming or counter operation is active")
        if len(config.channels) == 1:
            if int(config.rate_per_channel) != config.rate_per_channel:
                raise EvidenceRequiredError("single-channel rate must be an integer divisor of 24 MHz")
            return self.acquire_captured_ai0_reference(
                channel=config.channels[0],
                sample_rate_hz=int(config.rate_per_channel),
                sample_count=config.samples_per_channel,
                mode=config.mode,
                voltage_range=config.voltage_range,
                explicitly_authorized=True,
            )
        if config.mode is not TerminalMode.RSE or config.voltage_range != 10.0:
            raise EvidenceRequiredError("multichannel acquisition supports only RSE +/-10 V")
        if int(config.rate_per_channel) != config.rate_per_channel:
            raise EvidenceRequiredError("multichannel rate must be an integer divisor of 24 MHz")
        return self.acquire_captured_rse_multichannel(
            config.channels,
            sample_rate_hz=int(config.rate_per_channel),
            sample_count=config.samples_per_channel,
            explicitly_authorized=True,
        )

    def acquire_analog_volts(
        self, config: AnalogInputConfig, *, explicitly_authorized: bool = False
    ) -> CalibratedAiBatch:
        """Acquire raw bytes and NI-reference-equivalent calibrated voltages."""

        raw = self.acquire_analog(config, explicitly_authorized=explicitly_authorized)
        try:
            return decode_calibrated_scans(
                raw, config.channels, config.mode, tuple(self._last_ai_calibration_replies)
            )
        except ValueError as exc:
            raise ProtocolError(str(exc)) from exc

    def acquire_captured_ai0_reference(
        self,
        *,
        channel: int = 0,
        sample_rate_hz: int = 100,
        sample_count: int = 100,
        mode: TerminalMode = TerminalMode.RSE,
        voltage_range: float = 10.0,
        explicitly_authorized: bool = False,
    ) -> bytes:
        """Replay a bounded single-channel finite reference plan.

        This narrow path has no arbitrary command injection, output support,
        firmware operation, or voltage conversion.  It is deliberately opt-in
        even though every frame was captured from the same Base-15 firmware
        generation.  Returned bytes are raw EP2 data in wire order.
        """

        if not explicitly_authorized:
            raise EvidenceRequiredError(
                "captured AI replay is disabled: explicit authorization is required"
            )
        self._last_ai_calibration_replies.clear()
        if mode is TerminalMode.RSE:
            if voltage_range != 10.0:
                raise ValueError("RSE supports only +/-10 V")
            plan = captured_ai0_rse_finite_plan(
                channel=channel, sample_rate_hz=sample_rate_hz, sample_count=sample_count
            )
        elif mode is TerminalMode.DIFFERENTIAL:
            plan = captured_differential_finite_plan(
                channel=channel, voltage_range=voltage_range,
                sample_rate_hz=sample_rate_hz, sample_count=sample_count,
            )
        else:
            raise ValueError("unsupported terminal mode")
        if sample_count > 4096 or sample_count / sample_rate_hz > 10:
            raise ValueError("captured finite acquisition is bounded to 4096 samples and 10 seconds")
        timeout_ms = min(15_000, max(2_000, int(sample_count / sample_rate_hz * 2_000 + 2_000)))
        started = False
        try:
            for index, (frame, expected_reply) in enumerate(
                zip(plan.ep1_out_frames, plan.expected_ep1_replies, strict=True)
            ):
                if index == 20:
                    raw = self.transport.read(0x82, plan.expected_raw_bytes, timeout_ms)
                    if len(raw) != plan.expected_raw_bytes:
                        raise ProtocolError(
                            f"EP2 returned {len(raw)} bytes, expected {plan.expected_raw_bytes}"
                        )
                if self.transport.write(0x01, frame, timeout_ms=1_000) != len(frame):
                    raise ProtocolError(f"short EP1 write at captured frame {index}")
                reply = self.transport.read(0x81, 8192, timeout_ms=1_000)
                calibration_reply = (
                    index == 7 and len(reply) == 24
                    and reply.startswith(DIFFERENTIAL_CALIBRATION_REPLY_PREFIX)
                )
                if reply != expected_reply and not calibration_reply:
                    raise ProtocolError(f"unexpected EP1 reply at captured frame {index}: {reply.hex()}")
                if index == 7:
                    try:
                        decode_ai_calibration_reply(reply)
                    except ValueError as exc:
                        raise ProtocolError(str(exc)) from exc
                    self._last_ai_calibration_replies.append(reply)
                started = started or index >= 19
            return raw
        except Exception:
            # The observed frames after index 19 are the finite-run cleanup
            # tail.  Try that fixed tail once if an error interrupts a run.
            if started:
                for frame, expected_reply in zip(
                    plan.ep1_out_frames[20:], plan.expected_ep1_replies[20:], strict=True
                ):
                    try:
                        self.transport.write(0x01, frame, timeout_ms=1_000)
                        self.transport.read(0x81, 8192, timeout_ms=1_000)
                    except (Open6009Error, OSError, ValueError):
                        break
            raise

    def read_captured_digital_port(self, port: int, *, explicitly_authorized: bool = False) -> int:
        """Read one captured-supported digital input port; no output/direction writes."""

        if not explicitly_authorized:
            raise EvidenceRequiredError("digital input is disabled without explicit authorization")
        plan = captured_digital_input_port_plan(port)
        final_reply = b""
        for index, (frame, expected_reply) in enumerate(
            zip(plan.frames, plan.replies, strict=True)
        ):
            if self.transport.write(0x01, frame, timeout_ms=1_000) != len(frame):
                raise ProtocolError("short EP1 digital-input write")
            final_reply = self.transport.read(0x81, 8192, timeout_ms=1_000)
            if index == len(plan.frames) - 1:
                try:
                    decode_captured_port_reply(port, final_reply)
                except ValueError as exc:
                    raise ProtocolError(
                        f"unexpected digital-input reply: {final_reply.hex()}"
                    ) from exc
            elif final_reply != expected_reply:
                raise ProtocolError(f"unexpected digital-input reply: {final_reply.hex()}")
        return decode_captured_port_reply(port, final_reply)

    def read_captured_digital_pin(
        self, port: int, pin: int, *, explicitly_authorized: bool = False
    ) -> bool:
        """Read one input pin through the verified port-read transaction."""

        width = 8 if port == 0 else 4 if port == 1 else 0
        if not 0 <= pin < width:
            raise ValueError(f"pin {pin} is outside digital port {port}")
        return bool(
            self.read_captured_digital_port(port, explicitly_authorized=explicitly_authorized)
            & (1 << pin)
        )

    def acquire_captured_ai01_reference(self, *, explicitly_authorized: bool = False) -> bytes:
        """Run only the captured AI0+AI1/RSE 10-scan finite reference sequence."""

        if not explicitly_authorized:
            raise EvidenceRequiredError("captured multichannel AI replay requires authorization")
        frames = tuple(bytes.fromhex(x) for x in (
            "0000000c0008010000000000", "0000000c0008010b02000000", "0000000c0008010c02000000", "0000000c0008010d02000000", "0000000c0008010c02030000", "000000140010010f020200000006000000010000", "00000010000c010e0202000000040202", "0000001400100114020400000000011000080000", "0000001c001801100202000000002710ffffd8f0fdfd000400000000", "0000001400100114020400000000012000080000", "0000001c001801100202000000002710ffffd8f0fdfd000400010000", "00000010000c010e0200000001000000", "00000010000c0116020000000003a980", "0000000c0008011302000000", "0000000c0008011502000000", "00000010000c010f020000000000000a", "00000010000c01120203000000000028", "0000000c0008010f02030000", "0000000c0008010902030000", "0000000c0008010902000000", "0000000c0008010a02000000", "0000000c0008011002030000", "0000000c0008011002030000", "0000000c0008010b02000000", "0000000c0008010c02000000", "0000000c0008010c02030000", "0000000c0008010d02000000"))
        generic = bytes.fromhex("0000000c0008010000000002")
        replies = [generic] * len(frames)
        replies[0] = bytes.fromhex("0000001c0018010000000002020000000132c0000100000000020000")
        replies[7] = bytes.fromhex("000000180014010000000002000ac1246be64129c01dc124")
        replies[9] = bytes.fromhex("000000180014010000000002000ac12440a94129c22cc124")
        raw = b""
        for index, (frame, expected) in enumerate(zip(frames, replies, strict=True)):
            if index == 22:
                raw = self.transport.read(0x82, 40, timeout_ms=5_000)
                if len(raw) != 40:
                    raise ProtocolError(f"AI0+AI1 EP2 returned {len(raw)} bytes, expected 40")
            if self.transport.write(0x01, frame, timeout_ms=1_000) != len(frame):
                raise ProtocolError(f"short AI0+AI1 EP1 write at frame {index}")
            reply = self.transport.read(0x81, 8192, timeout_ms=1_000)
            if reply != expected:
                raise ProtocolError(f"unexpected AI0+AI1 reply at frame {index}: {reply.hex()}")
        return raw

    def acquire_captured_rse_multichannel(
        self,
        channels: tuple[int, ...],
        *,
        sample_rate_hz: int,
        sample_count: int,
        explicitly_authorized: bool = False,
    ) -> bytes:
        """Run a bounded, capture-derived 2–8 channel finite RSE acquisition."""

        if not explicitly_authorized:
            raise EvidenceRequiredError("multichannel AI requires explicit authorization")
        self._last_ai_calibration_replies.clear()
        if sample_rate_hz <= 0:
            raise ValueError("sample rate must be positive")
        plan = captured_rse_multichannel_plan(
            channels, sample_rate_hz=sample_rate_hz, sample_count=sample_count
        )
        raw = b""
        started = False
        next_index = 0
        timeout_ms = min(15_000, max(2_000, sample_count * 2_000 // sample_rate_hz + 2_000))
        try:
            for index, (frame, expected) in enumerate(
                zip(plan.ep1_out_frames, plan.expected_ep1_replies, strict=True)
            ):
                next_index = index
                if index == plan.ep2_before_frame:
                    raw = self.transport.read(0x82, plan.expected_raw_bytes, timeout_ms=timeout_ms)
                    if len(raw) != plan.expected_raw_bytes:
                        raise ProtocolError(
                            f"multichannel EP2 returned {len(raw)} bytes, expected {plan.expected_raw_bytes}"
                        )
                if self.transport.write(0x01, frame, timeout_ms=1000) != len(frame):
                    raise ProtocolError(f"short multichannel EP1 write at frame {index}")
                reply = self.transport.read(0x81, 8192, timeout_ms=1000)
                calibration_reply = (
                    index >= 7 and (index - 7) % 2 == 0
                    and index < 7 + 2 * len(channels)
                )
                if reply != expected and not calibration_reply:
                    raise ProtocolError(
                        f"unexpected multichannel EP1 reply at frame {index}: {reply.hex()}"
                    )
                if calibration_reply:
                    try:
                        decode_ai_calibration_reply(reply)
                    except ValueError as exc:
                        raise ProtocolError(str(exc)) from exc
                    self._last_ai_calibration_replies.append(reply)
                next_index = index + 1
                started = started or index >= 15 + 2 * len(channels)
            return raw
        except Exception:
            if started:
                for frame in plan.ep1_out_frames[max(next_index, plan.ep2_before_frame):]:
                    try:
                        self.transport.write(0x01, frame, timeout_ms=1000)
                        self.transport.read(0x81, 8192, timeout_ms=1000)
                    except (Open6009Error, OSError, ValueError):
                        break
            raise

    def close(self) -> None:
        try:
            try:
                self.stop_continuous_ai()
            finally:
                self.stop_counter()
        finally:
            self.transport.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def _replay_output_plan(self, plan: CapturedOutputPlan) -> None:
        for index, (frame, expected) in enumerate(zip(plan.frames, plan.replies, strict=True)):
            if self.transport.write(0x01, frame, timeout_ms=1000) != len(frame):
                raise ProtocolError(f"short output frame {index}")
            reply = self.transport.read(0x81, 8192, timeout_ms=1000)
            # This setup reply reports the port's existing value. NI's first
            # write ran while the lines read high (ff); subsequent writes can
            # legitimately return the value left by the preceding operation.
            dynamic_port_readback = (
                index == 1
                and frame[8:10] == b"\x02\x10"
                and len(reply) == len(expected) == 16
                and reply[:-2] == expected[:-2]
                and reply[-1] == expected[-1]
            )
            if reply != expected and not dynamic_port_readback:
                raise ProtocolError(f"unexpected output reply at frame {index}: {reply.hex()}")

    def set_captured_ao_zero(self, channel: int, *, explicitly_authorized: bool = False) -> None:
        """Write only the captured 0.0 V AO state for the specified channel."""

        if not explicitly_authorized:
            raise EvidenceRequiredError("AO zero operation requires explicit authorization")
        self._replay_output_plan(captured_ao_zero_plan(channel))

    def set_reference_ao_voltage(
        self, channel: int, voltage: float, *, explicitly_authorized: bool = False
    ) -> int:
        """Set 0–5 V using calibration endpoints read from this device.

        The USB command is hardware accepted. Electrical accuracy still
        requires a meter and a suitable output load.
        """

        if not explicitly_authorized:
            raise EvidenceRequiredError("analog output requires explicit authorization")
        if channel not in (0, 1):
            raise ValueError("AO channel must be 0 or 1")
        if not isinstance(voltage, (int, float)) or not isfinite(voltage) or not 0 <= voltage <= 5:
            raise ValueError("AO voltage must be finite and within 0..5 V")
        plan = captured_ao_zero_plan(channel)
        replies = []
        for index, frame in enumerate(plan.frames[:4]):
            if self.transport.write(0x01, frame, timeout_ms=1000) != len(frame):
                raise ProtocolError(f"short AO setup frame {index}")
            reply = self.transport.read(0x81, 8192, timeout_ms=1000)
            if index < 2 and reply != plan.replies[index]:
                raise ProtocolError(f"unexpected AO discovery reply: {reply.hex()}")
            replies.append(reply)
        try:
            calibration = decode_ao_calibration_replies(replies[3], replies[2])
        except ValueError as exc:
            raise ProtocolError(str(exc)) from exc
        code = ao_voltage_to_code(voltage, calibration)
        frame = captured_ao_code_plan(channel, code).frames[-1]
        if self.transport.write(0x01, frame, timeout_ms=1000) != len(frame):
            raise ProtocolError("short AO voltage frame")
        reply = self.transport.read(0x81, 8192, timeout_ms=1000)
        if reply != plan.replies[-1]:
            raise ProtocolError(f"unexpected AO voltage reply: {reply.hex()}")
        return code

    def set_captured_digital_port_zero(
        self, port: int, *, explicitly_authorized: bool = False
    ) -> None:
        """Write only the captured all-low digital state for the specified port."""

        if not explicitly_authorized:
            raise EvidenceRequiredError("digital output zero requires explicit authorization")
        self.write_digital_port(port, 0, explicitly_authorized=True)

    def write_digital_port(
        self, port: int, value: int, *, explicitly_authorized: bool = False
    ) -> None:
        """Write the captured port value field; caller controls output state."""

        if not explicitly_authorized:
            raise EvidenceRequiredError("digital port write requires explicit authorization")
        self._replay_output_plan(captured_digital_port_plan(port, value))
        self._digital_output_shadow[port] = value

    def write_digital_pin(
        self, port: int, pin: int, high: bool, *, explicitly_authorized: bool = False
    ) -> None:
        """Change one output bit while preserving this session's other outputs.

        A whole-port write must establish the shadow first. Reading the port
        would configure inputs, so it cannot safely recover the output state.
        """

        width = 8 if port == 0 else 4 if port == 1 else 0
        if not 0 <= pin < width:
            raise ValueError(f"pin {pin} is outside digital port {port}")
        if port not in self._digital_output_shadow:
            raise EvidenceRequiredError("set the whole digital output port before changing a pin")
        current = self._digital_output_shadow[port]
        value = current | (1 << pin) if high else current & ~(1 << pin)
        self.write_digital_port(port, value, explicitly_authorized=explicitly_authorized)

    def _counter_exchange(self, request: bytes, expected: bytes | None = None) -> bytes:
        if self.transport.write(0x01, request, timeout_ms=1000) != len(request):
            raise ProtocolError("short counter EP1 write")
        reply = self.transport.read(0x81, 8192, timeout_ms=1000)
        if expected is not None and reply != expected:
            raise ProtocolError(f"unexpected counter reply: {reply.hex()}")
        return reply

    def _continuous_exchange(self, request: bytes, expected: bytes, *, calibration: bool = False) -> bytes:
        if self.transport.write(0x01, request, timeout_ms=1000) != len(request):
            raise ProtocolError("short continuous-AI EP1 write")
        reply = self.transport.read(0x81, 8192, timeout_ms=1000)
        if calibration:
            try:
                decode_ai_calibration_reply(reply)
            except ValueError as exc:
                raise ProtocolError(str(exc)) from exc
        elif reply != expected:
            if reply == bytes.fromhex("00000010000c010000000006fffcf157"):
                raise AcquisitionOverflowError("USB-6009 onboard acquisition overflow (-200361)")
            raise ProtocolError(f"unexpected continuous-AI reply: {reply.hex()}")
        return reply

    def start_continuous_ai(
        self, *, channels: tuple[int, ...] = (0,), sample_rate_hz: int = 1000,
        explicitly_authorized: bool = False, async_ep2: bool | None = None
    ) -> None:
        """Start captured continuous RSE/+/-10 V acquisition."""

        if not explicitly_authorized:
            raise EvidenceRequiredError("continuous AI requires explicit authorization")
        if self._continuous_ai_plan is not None or self._counter_active:
            raise Open6009Error("another streaming or counter operation is active")
        if not 1 <= sample_rate_hz <= 48_000:
            raise ValueError("sample rate must be 1..48000 S/s")
        plan = captured_continuous_rse_plan(channels, sample_rate_hz)
        if async_ep2 is None:
            async_ep2 = isinstance(self.transport, LibusbTransport)
        if async_ep2 and not isinstance(self.transport, LibusbTransport):
            raise ValueError("asynchronous EP2 requires a libusb transport")
        calibration_replies: list[bytes] = []
        started = False
        try:
            for index, (frame, expected) in enumerate(
                zip(plan.start_frames, plan.start_replies, strict=True)
            ):
                if async_ep2 and index == len(plan.start_frames) - 1:
                    self._continuous_async_ep2 = self.transport.open_async_ep2()
                started = started or index >= len(plan.start_frames) - 3
                calibration = index >= 7 and (index - 7) % 2 == 0 and index < 7 + 2 * len(channels)
                reply = self._continuous_exchange(frame, expected, calibration=calibration)
                if calibration:
                    calibration_replies.append(reply)
        except Exception:
            if started:
                for frame, expected in zip(plan.stop_frames, plan.stop_replies, strict=True):
                    try:
                        self._continuous_exchange(frame, expected)
                    except (Open6009Error, OSError, ValueError):
                        break
            if self._continuous_async_ep2 is not None:
                self.transport.close_async_ep2(self._continuous_async_ep2)
                self._continuous_async_ep2 = None
            raise
        self._continuous_ai_plan = plan
        self._continuous_calibration_replies = tuple(calibration_replies)
        self._continuous_ai_total_scans = 0
        self._continuous_ai_buffer.clear()
        self._last_continuous_async_stats = {}

    def read_continuous_ai_volts(self, *, samples: int | None = None) -> CalibratedAiBatch:
        """Read a captured chunk size and apply per-channel NI calibration."""

        plan = self._continuous_ai_plan
        if plan is None or len(self._continuous_calibration_replies) != len(plan.channels):
            raise Open6009Error("continuous AI calibration is unavailable")
        raw = self.read_continuous_ai(samples=samples)
        return decode_calibrated_scans(
            raw, plan.channels, TerminalMode.RSE, self._continuous_calibration_replies
        )

    def read_continuous_ai(self, *, samples: int | None = None) -> bytes:
        """Read the next bounded scan-major raw chunk, preserving wire bytes."""

        plan = self._continuous_ai_plan
        if plan is None:
            raise Open6009Error("continuous AI must be started before reading")
        if samples is None:
            aggregate_rate = plan.sample_rate_hz * len(plan.channels)
            samples = 500 if aggregate_rate >= 20_000 else 100
        if samples not in (100, 250, 500, 1000):
            raise ValueError("only NI-captured 100, 250, 500, or 1000-scan chunks are supported")
        try:
            credit, status_before, status_after = plan.read_frames_for_total(
                self._continuous_ai_total_scans + samples
            )
            self._continuous_exchange(credit, _GENERIC_REPLY)
            self._continuous_exchange(status_before, _GENERIC_REPLY)
            deadline = monotonic() + max(2.0, samples / plan.sample_rate_hz * 3.0)
            needed = samples * 2 * len(plan.channels)
            zero_length_transfers = 0
            if self._continuous_async_ep2 is not None:
                self._continuous_ai_buffer.extend(
                    self._continuous_async_ep2.take(needed, deadline=deadline)
                )
            else:
                while len(self._continuous_ai_buffer) < needed:
                    remaining_ms = max(1, int((deadline - monotonic()) * 1000))
                    if deadline <= monotonic():
                        raise ProtocolError(
                            f"continuous EP2 timeout after {len(self._continuous_ai_buffer)} bytes"
                        )
                    # Bulk requests must end on a 64-byte packet boundary.
                    requested = ((needed - len(self._continuous_ai_buffer) + 63) // 64) * 64
                    part = self.transport.read(0x82, requested, timeout_ms=remaining_ms)
                    if not part:
                        zero_length_transfers += 1
                        if zero_length_transfers >= 64:
                            raise ProtocolError("repeated zero-length continuous EP2 transfers")
                        continue
                    zero_length_transfers = 0
                    self._continuous_ai_buffer.extend(part)
            self._continuous_exchange(status_after, _GENERIC_REPLY)
            raw = bytes(self._continuous_ai_buffer[:needed])
            del self._continuous_ai_buffer[:needed]
            self._continuous_ai_total_scans += samples
            return raw
        except Exception:
            try:
                self.stop_continuous_ai()
            except (Open6009Error, OSError, ValueError):
                pass
            raise

    def stop_continuous_ai(self) -> None:
        """Send the captured stop tail and clear local streaming state."""

        plan = self._continuous_ai_plan
        if plan is None:
            return
        self._continuous_ai_plan = None
        self._continuous_calibration_replies = ()
        self._continuous_ai_total_scans = 0
        self._continuous_ai_buffer.clear()
        first_error: Exception | None = None
        for frame, expected in zip(plan.stop_frames, plan.stop_replies, strict=True):
            try:
                self._continuous_exchange(frame, expected)
            except (Open6009Error, OSError, ValueError) as exc:
                if first_error is None:
                    first_error = exc
        if self._continuous_async_ep2 is not None:
            try:
                self.transport.close_async_ep2(self._continuous_async_ep2)
                self._last_continuous_async_stats = self._continuous_async_ep2.statistics
            except (Open6009Error, OSError, ValueError) as exc:
                if first_error is None:
                    first_error = exc
            self._continuous_async_ep2 = None
        # NI Base also consumes EP2 packets after its stop tail. Without this,
        # the next session can begin with stale bytes from the previous run.
        self._last_continuous_drain_bytes = 0
        if isinstance(self.transport, LibusbTransport):
            for _ in range(64):
                try:
                    part = self.transport.read(0x82, 64, timeout_ms=100)
                except UsbAccessError as exc:
                    if "LIBUSB_ERROR_TIMEOUT" not in str(exc) and first_error is None:
                        first_error = exc
                    break
                if not part:
                    break
                self._last_continuous_drain_bytes += len(part)
            else:
                if first_error is None:
                    first_error = ProtocolError("post-stop EP2 drain exceeded 4096 bytes")
        if first_error is not None:
            raise first_error

    @property
    def last_continuous_async_statistics(self) -> dict[str, int]:
        """Transport counters from the most recently stopped continuous run."""

        return dict(self._last_continuous_async_stats)

    def start_counter(self, *, explicitly_authorized: bool = False) -> None:
        """Start the captured falling-edge/count-up counter path."""

        if not explicitly_authorized:
            raise EvidenceRequiredError("counter start requires explicit authorization")
        if self._counter_active:
            raise Open6009Error("counter is already active")
        if self._continuous_ai_plan is not None:
            raise Open6009Error("continuous AI is active")
        self._counter_exchange(counter.DISCOVER, counter.DISCOVER_REPLY)
        try:
            self._counter_exchange(counter.CONFIGURE_FALLING_UP, counter.OK)
            self._counter_exchange(counter.START, counter.OK)
        except Exception:
            try:
                self._counter_exchange(counter.STOP, counter.OK)
            except (Open6009Error, OSError, ValueError):
                pass
            raise
        self._counter_active = True

    def read_counter_raw(self) -> tuple[bytes, int]:
        """Read the counter's four bytes and provisional big-endian value."""

        if not self._counter_active:
            raise Open6009Error("counter must be started before reading")
        reply = self._counter_exchange(counter.READ)
        try:
            return counter.decode_counter_reply(reply)
        except ValueError as exc:
            raise ProtocolError(str(exc)) from exc

    def stop_counter(self) -> None:
        """Stop the counter if this instance started it."""

        if not self._counter_active:
            return
        try:
            self._counter_exchange(counter.STOP, counter.OK)
        finally:
            self._counter_active = False
