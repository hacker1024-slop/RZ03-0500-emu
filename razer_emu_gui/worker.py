"""Single-owner emulator process; JSON lines are local GUI IPC, never USB replies."""

import asyncio
import json
import queue
import sys
import threading
import time
from pathlib import Path

from razer_emu import Bootloader, Firmware, FlashRegion
from razer_emu.core import Keyboard
from razer_emu.firmware import FLASH_APPLICATION

from .layout import key_colors, load_layout


def emit(message):
    print(json.dumps(message), flush=True)


def load_images(configuration, layout):
    full_image = configuration.get("full_image")
    if full_image:
        with Path(full_image).open("rb") as source:
            data = source.read(0x80001)
        if len(data) != 0x80000:
            raise ValueError("Expected a complete raw 512 KiB flash image")
        boot = Bootloader.from_bytes(data[:FLASH_APPLICATION])
        application_end = FLASH_APPLICATION + layout["firmware_size"]
        image = Firmware.from_bytes(data[FLASH_APPLICATION:application_end])
        regions = [FlashRegion(0, data, b"\x01" * len(data))]
    else:
        image = Firmware.load(configuration["firmware"])
        boot_path = configuration.get("bootloader")
        boot = Bootloader.load(boot_path) if boot_path else None
        regions = [FlashRegion.load_dump(path) for path in configuration["flash_dumps"]]
    if image.sha256 != layout["firmware_sha256"]:
        raise ValueError("This key/RGB map is verified only for application firmware 1.1.1")
    return image, boot, regions


async def run(configuration):
    layout = load_layout()
    image, boot, regions = load_images(configuration, layout)
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
        image,
        flash_regions=regions,
        bootloader=boot,
        check_interval_us=125,
    ) as keyboard:
        matrix = keyboard.attach_matrix()
        buttons = keyboard.attach_buttons()
        chips = [keyboard.attach_lighting(c["bus"], c["address"]) for c in layout["controllers"]]
        enumeration = keyboard.usb.enumerate_device()
        exported = configuration.get("usbip")
        pump = None
        endpoints = {
            address: (size, interval) for address, size, interval in enumeration.interrupt_inputs
        }

        def start_polling():
            from Antmicro.Renode.Peripherals.USB import RazerUsbInterruptPump

            backend = RazerUsbInterruptPump(keyboard._usb_device)
            for address, (size, interval) in endpoints.items():
                backend.Add(address, address & 15, size, 0, size, interval)
            return backend

        if not exported:
            pump = start_polling()
        transport = None
        if exported:
            from razer_emu.usbip import UsbIpServer

            transport = asyncio.create_task(UsbIpServer(keyboard, enumeration).serve(port=exported))
        held, releases = {}, {}
        running = True
        report = ""
        last_frame = 0.0
        state = keyboard.state()

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
                    if transport:
                        transport.cancel()
                        await asyncio.gather(transport, return_exceptions=True)
                    return
                if kind == "reset" and not exported:
                    release_all()
                    keyboard.reset()
                    enumeration = keyboard.usb.enumerate_device()
                    pump = start_polling()
                    report = ""
                    running = True
                elif kind == "running" and not exported:
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
            if transport and transport.done():
                transport.result()
            if running:
                state = keyboard.state() if exported else keyboard.run_for(5000, _usb_pump=pump)
                if state.stopped_reason:
                    raise RuntimeError(
                        state.stopped_reason + ": " + "; ".join(keyboard.logs()[-5:])
                    )
                if not exported and not keyboard._usb_device.Connected:
                    raise RuntimeError("Virtual USB device is disconnected")
                for completion in () if exported else pump.Drain():
                    endpoint = int(completion.Sequence)
                    if completion.Status:
                        raise RuntimeError(
                            f"USB input endpoint 0x{endpoint:02x}: {completion.Status}"
                        )
                    packet = bytes(completion.Data)
                    report = f"0x{endpoint:02x}  {packet.hex(' ')}"
                    emit({"type": "report", "endpoint": endpoint, "data": packet.hex()})
                    size, interval = endpoints[endpoint]
                    pump.Add(endpoint, endpoint & 15, size, 0, size, interval)
                for index, due in list(releases.items()):
                    if state.elapsed_us >= due:
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
                        "virtual_us": state.elapsed_us,
                        "wall_seconds": now,
                        "colors": key_colors(layout, [c.state() for c in chips]),
                        "pressed": list(held),
                        "report": report,
                    }
                )
                last_frame = now
            await asyncio.sleep(0.005 if exported else 0)


def main():
    try:
        asyncio.run(run(json.loads(sys.stdin.readline())))
    except Exception as exc:
        emit({"type": "error", "message": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
