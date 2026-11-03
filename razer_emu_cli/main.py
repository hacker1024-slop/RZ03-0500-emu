"""Headless frontend. All execution behavior belongs to the core library."""

import argparse
import asyncio
import json
import sys

from razer_emu.firmware import Bootloader, Firmware, FlashRegion
from razer_emu.inputs import InputScript
from razer_emu.usb import UsbError


def _lighting_address(value):
    try:
        bus, address = (int(part, 0) for part in value.split(":"))
        if bus not in (2, 4) or not 0x50 <= address <= 0x5F:
            raise ValueError
        return bus, address
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use BUS:ADDRESS, bus 2/4 and address 0x50–0x5F") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run original BlackWidow firmware in Renode")
    parser.add_argument("firmware", help="Raw application image with vector table")
    parser.add_argument(
        "--microseconds", type=int, default=1000, help="Virtual runtime (default 1000)"
    )
    parser.add_argument(
        "--check-interval-us",
        type=int,
        help="Fault-check interval, 1–1000 virtual us (default: 125 for USB/IP, otherwise 10)",
    )
    parser.add_argument(
        "--input-script",
        metavar="JSON",
        help="Play timestamped matrix/encoder/button inputs after USB setup (if requested)",
    )
    parser.add_argument(
        "--usb-enumerate",
        action="store_true",
        help="Attach a virtual host, enumerate/configure USB, then run the requested time slice",
    )
    parser.add_argument(
        "--usbip",
        type=int,
        metavar="PORT",
        help="Export firmware USB on 127.0.0.1:PORT until interrupted (typically 3240)",
    )
    parser.add_argument(
        "--bootloader-dump",
        metavar="PATH",
        help="Start from a complete bootloader binary or verified capture directory",
    )
    parser.add_argument(
        "--bootloader-mode", action="store_true",
        help="Hold Fn through reset and enumerate the Razer update-mode USB device",
    )
    parser.add_argument(
        "--flash-dump",
        action="append",
        default=[],
        metavar="DIRECTORY",
        help="Load a verified firmware-dump region (repeatable)",
    )
    parser.add_argument(
        "--lighting",
        action="append",
        type=_lighting_address,
        default=[],
        metavar="BUS:ADDRESS",
        help="Attach a CHROMA 192-compatible I2C target, e.g. 4:0x50 (repeatable)",
    )
    args = parser.parse_args(argv)
    if args.bootloader_mode and not args.bootloader_dump:
        parser.error("--bootloader-mode requires --bootloader-dump")
    if args.usbip is not None and not 1 <= args.usbip <= 65535:
        parser.error("USB/IP port must be 1 to 65535")
    if args.check_interval_us is not None and not 1 <= args.check_interval_us <= 1000:
        parser.error("Fault-check interval must be 1 to 1000 microseconds")
    try:
        script = InputScript.load(args.input_script) if args.input_script else None
        if args.usbip is None and not 1 <= args.microseconds <= 10_000_000:
            raise ValueError("--microseconds must be from 1 to 10,000,000")
        if args.usbip is None and script and script.duration_us > args.microseconds:
            raise ValueError("The input script extends beyond --microseconds")
        from razer_emu.core import Keyboard

        image = Firmware.load(args.firmware)
        with Keyboard(
            image,
            flash_regions=[FlashRegion.load_dump(p) for p in args.flash_dump],
            bootloader=Bootloader.load(args.bootloader_dump) if args.bootloader_dump else None,
            check_interval_us=(
                args.check_interval_us
                if args.check_interval_us is not None
                else 125
                if args.usbip is not None
                else 10
            ),
        ) as keyboard:
            playback = keyboard.prepare_input_script(script) if script else None
            lighting = [keyboard.attach_lighting(bus, address) for bus, address in args.lighting]
            enumerate_device = (keyboard.boot_into_bootloader if args.bootloader_mode
                                else keyboard.usb.enumerate_device)
            if args.usbip is not None:
                from razer_emu.usbip import UsbIpServer

                enumeration = enumerate_device()
                if playback:
                    playback.start()
                print(f"USB/IP listening on 127.0.0.1:{args.usbip}, bus ID 1-1", file=sys.stderr)
                try:
                    asyncio.run(UsbIpServer(keyboard, enumeration).serve(port=args.usbip))
                except KeyboardInterrupt:
                    pass
                return 0
            usb = usb_error = enumeration = None
            try:
                if args.usb_enumerate or args.bootloader_mode:
                    enumeration = enumerate_device()
                    usb = enumeration.to_dict()
            except UsbError as exc:
                usb_error = str(exc)
            reports = []
            if usb_error:
                state = keyboard.state()
            elif playback:
                playback.start()
                origin = keyboard.state().elapsed_us
                endpoints = enumeration.interrupt_inputs if enumeration else ()
                state = keyboard.state()
                try:
                    while (
                        state.elapsed_us - origin < args.microseconds and not state.stopped_reason
                    ):
                        for endpoint, size, interval in endpoints:
                            packet = keyboard.usb.poll_interrupt(
                                endpoint, size, interval_us=interval
                            )
                            if packet is not None:
                                reports.append(
                                    {
                                        "at_us": state.elapsed_us - origin,
                                        "endpoint": f"0x{endpoint:02x}",
                                        "data": packet.hex(),
                                    }
                                )
                        state = keyboard.run_for(
                            min(125, args.microseconds - state.elapsed_us + origin)
                        )
                except UsbError as exc:
                    usb_error = str(exc)
                    state = keyboard.state()
            else:
                state = keyboard.run_for(args.microseconds)
            result = {"firmware_sha256": image.sha256, **state.to_dict()}
            if playback:
                result["input_events_applied"] = playback.applied
                if enumeration:
                    result["usb_reports"] = reports
            if keyboard.bootloader is not None:
                result["bootloader_sha256"] = keyboard.bootloader.sha256
            if usb is not None:
                result["usb"] = usb
            if lighting:
                result["lighting"] = [
                    {"bus": chip.bus, "address": f"0x{chip.address:02x}", **chip.state().to_dict()}
                    for chip in lighting
                ]
            if usb_error:
                result["usb_error"] = usb_error
            print(json.dumps(result, indent=2))
            if state.stopped_reason or usb_error:
                if usb_error:
                    print(usb_error, file=sys.stderr)
                print("\n".join(keyboard.logs()), file=sys.stderr)
                return 2
        return 0
    except (ValueError, OSError, RuntimeError, ImportError) as exc:
        print(f"razer-emu: {exc}", file=sys.stderr)
        return 1
