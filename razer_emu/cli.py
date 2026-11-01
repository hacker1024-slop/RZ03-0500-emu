"""Headless frontend. All execution behavior belongs to the core library."""

import argparse
import json
import sys

from .firmware import Firmware


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run original BlackWidow firmware in Renode")
    parser.add_argument("firmware", help="Raw application image with vector table")
    parser.add_argument(
        "--microseconds", type=int, default=1000, help="Virtual runtime (default 1000)"
    )
    args = parser.parse_args(argv)
    try:
        from .core import Keyboard

        image = Firmware.load(args.firmware)
        with Keyboard(image) as keyboard:
            state = keyboard.run_for(args.microseconds)
            print(json.dumps({"firmware_sha256": image.sha256, **state.to_dict()}, indent=2))
            if state.stopped_reason:
                print("\n".join(keyboard.logs()), file=sys.stderr)
                return 2
        return 0
    except (ValueError, OSError, RuntimeError, ImportError) as exc:
        print(f"razer-emu: {exc}", file=sys.stderr)
        return 1
