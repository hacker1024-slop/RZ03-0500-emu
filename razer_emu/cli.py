"""Headless frontend. All execution behavior belongs to the core library."""

import argparse
import json
import sys

from .firmware import Firmware, FlashRegion
from .usb import UsbError


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
        "--flash-dump",
        action="append",
        default=[],
        metavar="DIRECTORY",
        help="Load a verified firmware-dump region (repeatable)",
    )
    args = parser.parse_args(argv)
    try:
        from .core import Keyboard

        image = Firmware.load(args.firmware)
        with Keyboard(
            image, flash_regions=[FlashRegion.load_dump(p) for p in args.flash_dump]
        ) as keyboard:
            usb = usb_error = None
            try:
                if args.usb_enumerate:
                    usb = keyboard.usb.enumerate_device().to_dict()
            except UsbError as exc:
                usb_error = str(exc)
            state = keyboard.state() if usb_error else keyboard.run_for(args.microseconds)
            result = {"firmware_sha256": image.sha256, **state.to_dict()}
            if usb is not None:
                result["usb"] = usb
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
