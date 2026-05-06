from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from ...evdev import ecodes
from ..commands import info, ok
from .collector import capture_device
from .linux import DeviceCaptureError

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ENVIRONMENT = 3
EXIT_INTERRUPTED = 130


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bluetooth_2_usb device", description="Device support tooling for collecting source input-device captures."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser(
        "capture",
        help="Capture static device metadata plus summarized live evdev and hidraw evidence.",
        description=(
            "Capture local evidence for adding support for a Linux input device. "
            "Summarized mode keeps compact support data by default; raw mode emits every live event and report. "
            "The artifact may contain typed keys, report bytes, MAC addresses, and unique device IDs."
        ),
    )
    capture.add_argument(
        "--device",
        required=True,
        help="Input device path, Bluetooth MAC, or name fragment. Multiple matches are captured together.",
    )
    capture.add_argument("--duration", type=_positive_int, default=30, help="Capture duration in seconds. Default: 30")
    capture.add_argument(
        "--output",
        type=Path,
        default=None,
        help=("JSONL output path. Default: " "./device_capture/device_capture_<matched-device-name>_<timestamp>.jsonl"),
    )
    capture.add_argument("--format", choices=["jsonl"], default="jsonl", help="Output format. Default: jsonl")
    capture.add_argument(
        "--grab", action="store_true", help="Exclusively grab all matched input event devices during capture."
    )
    capture.add_argument(
        "--include-hidraw",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Also capture matching hidraw reports when available. Default: enabled",
    )
    capture.add_argument(
        "--live-mode",
        choices=["summarized", "raw"],
        default="summarized",
        help="Live data retention mode. summarized keeps compact support snapshots; raw emits every event/report. Default: summarized",
    )
    capture.add_argument(
        "--max-report-bytes",
        type=_positive_int,
        default=4096,
        help="Maximum bytes retained from one hidraw report. Default: 4096",
    )
    capture.add_argument(
        "--max-sysfs-file-bytes",
        type=_positive_int,
        default=65536,
        help="Maximum bytes retained from one sysfs metadata file. Default: 65536",
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command != "capture":
        parser.error(f"Unhandled command: {args.command}")

    progress = _CliProgress()
    try:
        path = asyncio.run(
            capture_device(
                selector=args.device,
                duration_sec=args.duration,
                output_path=args.output,
                grab=args.grab,
                include_hidraw=args.include_hidraw,
                max_report_bytes=args.max_report_bytes,
                max_sysfs_file_bytes=args.max_sysfs_file_bytes,
                live_mode=args.live_mode,
                progress=progress,
            )
        )
    except KeyboardInterrupt:
        progress.finish_line()
        if progress.output_path is not None:
            print(f"Device capture interrupted; wrote partial capture: {progress.output_path}", file=sys.stderr)
        else:
            print("Device capture interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except DeviceCaptureError as exc:
        progress.finish_line()
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        progress.finish_line()
        print(f"Device capture failed: {exc}", file=sys.stderr)
        return EXIT_ENVIRONMENT
    progress.finish_line()
    ok(f"Wrote: {path}")
    return EXIT_OK


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


@dataclass
class _CliProgress:
    output_path: Path | None = None
    evdev_events: int = 0
    hidraw_reports: int = 0
    key_codes: set[str] = field(default_factory=set)
    rel_codes: set[str] = field(default_factory=set)
    abs_codes: set[str] = field(default_factory=set)
    hidraw_paths: set[str] = field(default_factory=set)
    _last_render_monotonic: float = 0.0
    _line_active: bool = False

    def capture_started(self, devices, output_path: Path) -> None:
        self.output_path = output_path
        summaries = ", ".join(f"{getattr(device, 'path', '')} ({getattr(device, 'name', '')})" for device in devices)
        info(f"Capturing these matching devices: {summaries}")
        info("Waiting for input; stop capturing with Ctrl-C.")
        self._render(force=True)

    def evdev_event(self, device, event: object) -> None:
        self.evdev_events += 1
        event_type = getattr(event, "type", None)
        code_name = _event_code_label(event)
        if event_type == ecodes.EV_KEY:
            self.key_codes.add(code_name)
        elif event_type == ecodes.EV_REL:
            self.rel_codes.add(code_name)
        elif event_type == ecodes.EV_ABS:
            self.abs_codes.add(code_name)
        self._render()

    def hidraw_report(self, path: Path, report: bytes) -> None:
        self.hidraw_reports += 1
        self.hidraw_paths.add(f"{path.name}:{len(report)}B")
        self._render()

    def capture_finished(self, output_path: Path, *, interrupted: bool) -> None:
        self.output_path = output_path
        self._render(force=True)

    def finish_line(self) -> None:
        if self._line_active:
            print(file=sys.stderr)
            self._line_active = False

    def _render(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_render_monotonic < 0.25:
            return
        self._last_render_monotonic = now
        message = (
            f"events={self.evdev_events} keys/buttons={len(self.key_codes)} "
            f"rel={len(self.rel_codes)} abs={len(self.abs_codes)} "
            f"hidraw_reports={self.hidraw_reports} hidraw_groups={len(self.hidraw_paths)}"
        )
        print(f"\r{message}", end="", file=sys.stderr, flush=True)
        self._line_active = True


def _event_code_label(event: object) -> str:
    event_type = getattr(event, "type", None)
    code = getattr(event, "code", None)
    if isinstance(event_type, int) and isinstance(code, int):
        return _event_code_name(event_type, code)
    return str(code)


@lru_cache(maxsize=512)
def _event_code_name(event_type: int, code: int) -> str:
    prefixes = {
        getattr(ecodes, "EV_KEY", None): ("KEY_", "BTN_"),
        getattr(ecodes, "EV_REL", None): ("REL_",),
        getattr(ecodes, "EV_ABS", None): ("ABS_",),
    }.get(event_type, ())
    for name in dir(ecodes):
        if name.startswith(prefixes) and getattr(ecodes, name) == code:
            return name
    return str(code)
