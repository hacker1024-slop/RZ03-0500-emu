"""Headless frontend. All execution behavior belongs to the core library."""

import argparse
import asyncio
import json
import sys

from .firmware import Bootloader, Firmware, FlashRegion
from .usb import UsbError


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
        metavar="DIRECTORY",
        help="Start from a complete verified bootloader capture instead of the application handoff",
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
    if args.usbip is not None and not 1 <= args.usbip <= 65535:
        parser.error("USB/IP port must be 1 to 65535")
    try:
        from .core import Keyboard

        image = Firmware.load(args.firmware)
        with Keyboard(
            image,
            flash_regions=[FlashRegion.load_dump(p) for p in args.flash_dump],
            bootloader=Bootloader.load_dump(args.bootloader_dump) if args.bootloader_dump else None,
        ) as keyboard:
            lighting = [keyboard.attach_lighting(bus, address) for bus, address in args.lighting]
            if args.usbip is not None:
                from .usbip import UsbIpServer

                enumeration = keyboard.usb.enumerate_device()
                print(f"USB/IP listening on 127.0.0.1:{args.usbip}, bus ID 1-1", file=sys.stderr)
                try:
                    asyncio.run(UsbIpServer(keyboard, enumeration).serve(port=args.usbip))
                except KeyboardInterrupt:
                    pass
                return 0
            usb = usb_error = None
            try:
                if args.usb_enumerate:
                    usb = keyboard.usb.enumerate_device().to_dict()
            except UsbError as exc:
                usb_error = str(exc)
            state = keyboard.state() if usb_error else keyboard.run_for(args.microseconds)
            result = {"firmware_sha256": image.sha256, **state.to_dict()}
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
