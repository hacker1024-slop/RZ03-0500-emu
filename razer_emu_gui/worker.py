"""Single-owner emulator process; JSON lines are local GUI IPC, never USB replies."""

import json
import queue
import sys
import threading
import time

from razer_emu import Bootloader, Firmware, FlashRegion
from razer_emu.core import Keyboard

from .layout import key_colors, load_layout


def emit(message):
    print(json.dumps(message), flush=True)


def run(configuration):
    layout = load_layout()
    image = Firmware.load(configuration["firmware"])
    if image.sha256 != layout["firmware_sha256"]:
        raise ValueError("This key/RGB map is verified only for application firmware 1.1.1")
    regions = [FlashRegion.load_dump(path) for path in configuration["flash_dumps"]]
    boot = configuration.get("bootloader")
    commands = queue.Queue()

    def receive():
        for line in sys.stdin:
            try:
                commands.put(json.loads(line))
            except ValueError:
                commands.put({"type": "quit"})
        commands.put({"type": "quit"})

    threading.Thread(target=receive, daemon=True).start()
    with Keyboard(
        image, flash_regions=regions, bootloader=Bootloader.load_dump(boot) if boot else None
    ) as keyboard:
        matrix = keyboard.attach_matrix()
        buttons = keyboard.attach_buttons()
        chips = [keyboard.attach_lighting(c["bus"], c["address"]) for c in layout["controllers"]]
        enumeration = keyboard.usb.enumerate_device()
        held, releases = {}, {}
        running = True
        report = ""
        last_frame = 0.0

        def contact(index, pressed):
            key = layout["keys"][index]
            if key["matrix"] is not None:
                matrix.set_key(*key["matrix"], pressed)
            else:
                buttons.set_pressed(key["button"], pressed)

        def release_all():
            for index in held:
                contact(index, False)
            held.clear()
            releases.clear()

        while True:
            while not commands.empty():
                command = commands.get_nowait()
                kind = command["type"]
                if kind == "quit":
                    return
                if kind == "reset":
                    release_all()
                    keyboard.reset()
                    enumeration = keyboard.usb.enumerate_device()
                    report = ""
                    running = True
                elif kind == "running":
                    running = bool(command["value"])
                    if not running:
                        release_all()
                elif kind == "release_all":
                    release_all()
                elif kind == "key" and running:
                    index = command["index"]
                    if type(index) is not int or not 0 <= index < len(layout["keys"]):
                        raise ValueError("Key outside the selected layout")
                    if command["pressed"]:
                        if index not in held:
                            contact(index, True)
                            held[index] = keyboard.state().elapsed_us
                        releases.pop(index, None)
                    elif index in held:
                        # A short mouse click must still cross firmware debounce in
                        # virtual time, even when emulation runs below real time.
                        releases[index] = max(keyboard.state().elapsed_us, held[index] + 40_000)
            if running:
                state = keyboard.run_for(1000)
                if state.stopped_reason:
                    raise RuntimeError(
                        state.stopped_reason + ": " + "; ".join(keyboard.logs()[-5:])
                    )
                for endpoint, size, interval in enumeration.interrupt_inputs:
                    packet = keyboard.usb.poll_interrupt(endpoint, size, interval_us=interval)
                    if packet is not None:
                        report = f"0x{endpoint:02x}  {packet.hex(' ')}"
                        emit({"type": "report", "endpoint": endpoint, "data": packet.hex()})
                for index, due in list(releases.items()):
                    if keyboard.state().elapsed_us >= due:
                        contact(index, False)
                        del held[index]
                        del releases[index]
            else:
                time.sleep(0.01)
            now = time.monotonic()
            if now - last_frame >= 0.05:
                emit(
                    {
                        "type": "frame",
                        "running": running,
                        "virtual_us": keyboard.state().elapsed_us,
                        "colors": key_colors(layout, [c.state() for c in chips]),
                        "pressed": list(held),
                        "report": report,
                    }
                )
                last_frame = now


def main():
    try:
        run(json.loads(sys.stdin.readline()))
    except Exception as exc:
        emit({"type": "error", "message": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
