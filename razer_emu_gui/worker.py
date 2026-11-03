"""Single-owner emulator process; JSON lines are local GUI IPC, never USB replies."""

import asyncio
import json
import queue
import sys
import threading
import time
from collections import deque
from pathlib import Path

from razer_emu import Bootloader, Firmware, FlashRegion
from razer_emu.core import Keyboard
from razer_emu.firmware import FLASH_APPLICATION, SRAM_SIZE

from .layout import key_colors, load_layout

# Start and end at idle. The firmware samples each phase for 5 ms.
WHEEL_DOWN = ((True, False), (False, False), (False, True), (True, True))
WHEEL_UP = tuple(reversed(WHEEL_DOWN[:-1])) + (WHEEL_DOWN[-1],)
WHEEL_PHASE_US = 5000
MAX_WHEEL_PHASES = 128


def emit(message):
    print(json.dumps(message), flush=True)


def load_images(configuration):
    full_image = configuration.get("full_image")
    if full_image:
        with Path(full_image).open("rb") as source:
            data = source.read(0x80001)
        if len(data) != 0x80000:
            raise ValueError("Expected a complete raw 512 KiB flash image")
        boot = Bootloader.from_bytes(data[:FLASH_APPLICATION])
        # Validate the application vectors within the maximum SRAM image span.
        # In cold boot the bootloader determines what to copy from flash.
        application_end = FLASH_APPLICATION + SRAM_SIZE
        image = Firmware.from_bytes(data[FLASH_APPLICATION:application_end])
        regions = [FlashRegion(0, data, b"\x01" * len(data))]
    else:
        image = Firmware.load(configuration["firmware"])
        boot_path = configuration.get("bootloader")
        boot = Bootloader.load(boot_path) if boot_path else None
        # Blank data with valid ECC lets the application take its default
        # settings path. Physically erased words instead fault on CPU reads.
        settings_start, flash_end = 0x44000, 0x80000
        regions = [FlashRegion.load_dump(path) for path in configuration["flash_dumps"]]
        blank_mask = bytearray(b"\x01" * (flash_end - settings_start))
        for capture in regions:
            start = max(settings_start, capture.address)
            end = min(flash_end, capture.address + len(capture.data))
            if start < end:
                blank_mask[start - settings_start:end - settings_start] = bytes(end - start)
        regions.insert(0, FlashRegion(settings_start, bytes(len(blank_mask)), blank_mask))
    return image, boot, regions


async def run(configuration):
    layout = load_layout()
    image, boot, regions = load_images(configuration)
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
        encoder = keyboard.attach_encoder()
        chips = [keyboard.attach_lighting(c["bus"], c["address"]) for c in layout["controllers"]]
        enumeration = keyboard.usb.enumerate_device()
        exported = configuration.get("usbip")
        limit_speed = configuration.get("limit_speed", True)
        pump = None
        pace_deadline = None

        async def pace(started, before, after):
            nonlocal pace_deadline
            if not limit_speed or after == before:
                return
            # Carry ordinary timer overshoot into the next batch. Rebase after
            # a longer gap so idle time cannot accumulate as speed credit.
            if pace_deadline is None or started - pace_deadline > 0.010:
                pace_deadline = started
            pace_deadline += (after - before) / 1_000_000
            delay = pace_deadline - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)

        endpoints = {
            address: (size, interval) for address, size, interval in enumeration.interrupt_inputs
        }

        def start_polling():
            from Antmicro.Renode.Peripherals.USB import RazerUsbInterruptPump

            backend = RazerUsbInterruptPump(keyboard._usb_device)
            for address, (size, interval) in endpoints.items():
                backend.Add(address, address & 15, size, 0, size, interval)
            return backend

        def start_export():
            from razer_emu.usbip import UsbIpServer

            server = UsbIpServer(keyboard, enumeration, pace=pace)
            return server, asyncio.create_task(server.serve(port=exported))

        async def stop_export():
            if transport is not None:
                transport.cancel()
                await asyncio.gather(transport, return_exceptions=True)

        server = transport = None
        if exported:
            server, transport = start_export()
        else:
            pump = start_polling()
        wheel_phases = deque()
        next_phase = 0
        held, releases = {}, {}
        momentary, latched = set(), set()
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

        def release_all(*, keep_latched=False):
            momentary.clear()
            if not keep_latched:
                latched.clear()
            for index in held.keys() - latched:
                contact(index, False)
                del held[index]
                releases.pop(index, None)

        def clear_wheel():
            nonlocal next_phase
            wheel_phases.clear()
            encoder.set_phases(True, True)
            next_phase = 0

        while True:
            while not commands.empty():
                command = commands.get_nowait()
                kind = command["type"]
                if kind == "quit":
                    await stop_export()
                    return
                if kind == "reset":
                    pace_deadline = None
                    await stop_export()
                    release_all()
                    clear_wheel()
                    keyboard.reset()
                    enumeration = keyboard.usb.enumerate_device()
                    if exported:
                        server, transport = start_export()
                    else:
                        pump = start_polling()
                    report = ""
                    running = True
                elif kind == "limit_speed":
                    limit_speed = command["enabled"]
                    pace_deadline = None
                elif kind == "running" and not exported:
                    running = bool(command["value"])
                    pace_deadline = None
                    if not running:
                        release_all()
                        clear_wheel()
                elif kind == "wheel" and running:
                    cycle = WHEEL_UP if command["direction"] > 0 else WHEEL_DOWN
                    if len(wheel_phases) + len(cycle) <= MAX_WHEEL_PHASES:
                        wheel_phases.extend(cycle)
                elif kind == "release_all":
                    release_all(keep_latched=True)
                elif kind in ("key", "toggle_key") and running:
                    index = command["index"]
                    if type(index) is not int or not 0 <= index < len(layout["keys"]):
                        raise ValueError("Key outside the selected layout")
                    if kind == "toggle_key":
                        latched.symmetric_difference_update((index,))
                    elif command["pressed"]:
                        momentary.add(index)
                    else:
                        momentary.discard(index)
                    if index in momentary or index in latched:
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
            guest_attached = server is not None and server.imported
            if running:
                if wheel_phases:
                    elapsed = keyboard.state().elapsed_us
                    if elapsed >= next_phase:
                        encoder.set_phases(*wheel_phases.popleft())
                        next_phase = elapsed + WHEEL_PHASE_US
                # An attached guest drives execution through USB/IP transfers.
                # While waiting for a guest, advance firmware without polling USB.
                if guest_attached:
                    state = keyboard.state()
                else:
                    if limit_speed:
                        started = time.monotonic()
                        before = keyboard.state().elapsed_us
                    state = keyboard.run_for(5000, _usb_pump=pump)
                    if limit_speed:
                        await pace(started, before, state.elapsed_us)
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
            await asyncio.sleep(0.005 if guest_attached else 0)


def main():
    try:
        asyncio.run(run(json.loads(sys.stdin.readline())))
    except Exception as exc:
        emit({"type": "error", "message": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
