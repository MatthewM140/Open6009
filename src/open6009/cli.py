"""Command-line access to the production Open6009 driver."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from contextlib import nullcontext
from pathlib import Path

from .acquisition.config import AnalogInputConfig, TerminalMode
from .descriptors import discover_sysfs
from .device import Usb6009
from .diagnostics import collect_diagnostics, explain_access, report_as_dict
from .errors import EvidenceRequiredError, Open6009Error, ProtocolError


def _channels(value: str) -> tuple[int, ...]:
    parts = tuple(part.strip().lower() for part in value.split(","))
    if not parts or any(not part.startswith("ai") or not part[2:].isdigit() for part in parts):
        raise argparse.ArgumentTypeError("use comma-separated channel names such as ai0,ai1")
    return tuple(int(part[2:]) for part in parts)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="open6009", description="Native NI USB-6009 driver")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("devices", help="list connected USB-6009 devices")
    commands.add_parser("diagnose", help="report USB discovery and permissions")
    commands.add_parser("gui", help="start the Open6009 desktop application")

    acquire = commands.add_parser("acquire", help="run a bounded finite analog acquisition")
    acquire.add_argument("channels", type=_channels, help="for example ai0 or ai0,ai1")
    acquire.add_argument("--mode", choices=tuple(mode.value for mode in TerminalMode),
                         default=TerminalMode.RSE.value)
    acquire.add_argument("--range", dest="voltage_range", type=float, default=10.0)
    acquire.add_argument("--samples", type=int, default=100)
    acquire.add_argument("--rate", type=int, default=1000, help="samples/s per channel")
    acquire.add_argument("--raw-output", type=Path, required=True)
    acquire.add_argument("--csv-output", type=Path)
    acquire.add_argument("--execute-native", action="store_true",
                         help="required acknowledgement for physical USB I/O")

    stream = commands.add_parser("stream", help="run a bounded continuous RSE acquisition")
    stream.add_argument("channels", type=_channels, help="for example ai0,ai1")
    stream.add_argument("--rate", type=int, default=1000, help="samples/s per channel")
    stream.add_argument("--chunks", type=int, default=10)
    stream.add_argument("--samples-per-chunk", type=int, choices=(100, 250, 500, 1000))
    stream.add_argument("--raw-output", type=Path, required=True)
    stream.add_argument("--csv-output", type=Path)
    stream.add_argument("--execute-native", action="store_true")

    read = commands.add_parser("digital-read", help="read digital port 0 or 1")
    read.add_argument("port", type=int, choices=(0, 1))
    read.add_argument("--execute-native", action="store_true")
    write = commands.add_parser("digital-write", help="write an entire open-collector port")
    write.add_argument("port", type=int, choices=(0, 1))
    write.add_argument("value", type=lambda item: int(item, 0))
    write.add_argument("--execute-native", action="store_true")
    ao = commands.add_parser("ao-set", help="set AO0 or AO1 to 0..5 V")
    ao.add_argument("channel", type=int, choices=(0, 1))
    ao.add_argument("voltage", type=float)
    ao.add_argument("--execute-native", action="store_true")
    counter = commands.add_parser("counter-read", help="start, read, and stop CTR0")
    counter.add_argument("--execute-native", action="store_true")
    return parser


def _ensure_new(*paths: Path | None) -> None:
    for path in paths:
        if path is not None and (path.exists() or path.with_name(path.name + ".partial").exists()):
            raise ValueError(f"refusing to overwrite existing output: {path}")


def _write_csv(path: Path, channels: tuple[int, ...], scans: tuple[tuple[float, ...], ...],
               rate: int) -> None:
    with path.open("w", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(("time_s", *(f"ai{channel}_V" for channel in channels)))
        for offset, values in enumerate(scans):
            writer.writerow((offset / rate, *(format(value, ".12g") for value in values)))


def _devices() -> int:
    devices = [{
        "product": item.product or "NI USB-6009",
        "serial": item.serial,
        "vid": f"{item.vendor_id:04x}",
        "pid": f"{item.product_id:04x}",
        "bus": item.bus_number,
        "address": item.device_number,
        "usbfs_path": str(item.usbfs_path),
    } for item in discover_sysfs()]
    print(json.dumps(devices, indent=2))
    return 0 if devices else 2


def _diagnose() -> int:
    data = report_as_dict(collect_diagnostics())
    for record in data["sysfs_devices"]:
        record["access_assessment"] = explain_access(record)
    print(json.dumps(data, indent=2))
    return 0 if data["sysfs_devices"] else 2


def _finite(args: argparse.Namespace) -> int:
    if not args.execute_native:
        raise EvidenceRequiredError("acquire requires --execute-native")
    _ensure_new(args.raw_output, args.csv_output)
    config = AnalogInputConfig(args.channels, TerminalMode(args.mode), args.voltage_range,
                               args.samples, args.rate)
    with Usb6009.open() as device:
        batch = device.acquire_analog_volts(config, explicitly_authorized=True)
    raw_partial = args.raw_output.with_name(args.raw_output.name + ".partial")
    raw_partial.write_bytes(batch.raw_bytes)
    raw_partial.replace(args.raw_output)
    if args.csv_output is not None:
        csv_partial = args.csv_output.with_name(args.csv_output.name + ".partial")
        _write_csv(csv_partial, batch.channels, batch.scans_volts, args.rate)
        csv_partial.replace(args.csv_output)
    print(json.dumps({"channels": batch.channels, "scans": batch.samples_per_channel,
                      "raw_output": str(args.raw_output),
                      "csv_output": str(args.csv_output) if args.csv_output else None}))
    return 0


def _stream(args: argparse.Namespace) -> int:
    if not args.execute_native:
        raise EvidenceRequiredError("stream requires --execute-native")
    if not 1 <= args.chunks <= 10_000:
        raise ValueError("chunks must be 1..10000")
    samples = args.samples_per_chunk or (500 if args.rate * len(args.channels) >= 20_000 else 100)
    AnalogInputConfig(args.channels, TerminalMode.RSE, 10.0, samples, args.rate)
    _ensure_new(args.raw_output, args.csv_output)
    raw_partial = args.raw_output.with_name(args.raw_output.name + ".partial")
    csv_partial = (args.csv_output.with_name(args.csv_output.name + ".partial")
                   if args.csv_output else None)
    total = 0
    with Usb6009.open() as device:
        device.start_continuous_ai(channels=args.channels, sample_rate_hz=args.rate,
                                   explicitly_authorized=True)
        with (raw_partial.open("wb") as raw_file,
              (csv_partial.open("w", newline="") if csv_partial else nullcontext()) as csv_file):
            writer = csv.writer(csv_file) if csv_file else None
            if writer:
                writer.writerow(("time_s", *(f"ai{channel}_V" for channel in args.channels)))
            for _ in range(args.chunks):
                batch = device.read_continuous_ai_volts(samples=samples)
                raw_file.write(batch.raw_bytes)
                if writer:
                    for offset, values in enumerate(batch.scans_volts):
                        writer.writerow(((total + offset) / args.rate,
                                         *(format(value, ".12g") for value in values)))
                total += batch.samples_per_channel
        device.stop_continuous_ai()
        statistics = device.last_continuous_async_statistics
    raw_partial.replace(args.raw_output)
    if csv_partial:
        csv_partial.replace(args.csv_output)
    print(json.dumps({"channels": args.channels, "scans": total,
                      "raw_output": str(args.raw_output), "transport": statistics}))
    return 0


def _hardware_control(args: argparse.Namespace) -> int:
    if not args.execute_native:
        raise EvidenceRequiredError(f"{args.command} requires --execute-native")
    with Usb6009.open() as device:
        if args.command == "digital-read":
            value = device.read_captured_digital_port(args.port, explicitly_authorized=True)
            result = {"port": args.port, "value": value, "hex": f"0x{value:02x}"}
        elif args.command == "digital-write":
            device.write_digital_port(args.port, args.value, explicitly_authorized=True)
            result = {"port": args.port, "written": args.value}
        elif args.command == "ao-set":
            code = device.set_reference_ao_voltage(args.channel, args.voltage,
                                                   explicitly_authorized=True)
            result = {"channel": args.channel, "requested_voltage": args.voltage,
                      "dac_code": code}
        else:
            device.start_counter(explicitly_authorized=True)
            raw, value = device.read_counter_raw()
            result = {"count": value, "raw_bytes": raw.hex()}
    print(json.dumps(result))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "devices":
            return _devices()
        if args.command == "diagnose":
            return _diagnose()
        if args.command == "gui":
            from .gui.window import run
            return run()
        if args.command == "acquire":
            return _finite(args)
        if args.command == "stream":
            return _stream(args)
        return _hardware_control(args)
    except KeyboardInterrupt:
        print("cancelled; USB cleanup completed", file=sys.stderr)
        return 130
    except (Open6009Error, ProtocolError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
