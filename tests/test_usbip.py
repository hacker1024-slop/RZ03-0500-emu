"""USB/IP wire interoperability and cancellation, independent of Renode."""

import asyncio
import errno
import struct
from collections import deque
from types import SimpleNamespace

from razer_emu.usb import UsbEnumeration, UsbStall
from razer_emu.usbip import DEVICE_ID, UsbIpServer


def _enumeration():
    device = bytes.fromhex("12010002000000403215a502010100000001")
    configuration = bytes.fromhex(
        "090222000101008032090400000103010100092111010001223d0007058103080001"
    )
    return UsbEnumeration(device, configuration)


class _Keyboard:
    def __init__(self):
        self.usb = self
        self.time = 0
        self.packets = deque()
        self.requests = []

    def run_for(self, duration):
        self.time += duration

    def _check(self):
        return SimpleNamespace(elapsed_us=self.time)

    def reset_bus(self):
        self.packets.clear()

    def control(self, setup, payload=b""):
        self.requests.append((setup, payload))
        if setup.value == 0x9900:
            raise UsbStall("unsupported descriptor")
        return _enumeration().device[: setup.length] if setup.request_type & 128 else b""

    def poll_interrupt(self, endpoint, size, *, interval_us):
        return self.packets.popleft() if self.packets else None


def _submit(sequence, *, endpoint=0, direction=1, length=18, setup=None, flags=0):
    setup = setup or struct.pack("<BBHHH", 0x80, 6, 0x100, 0, length)
    return struct.pack(
        "!IIIIIIiiii8s", 1, sequence, DEVICE_ID, direction, endpoint, flags, length, 0, -1, 0, setup
    )


async def _reply(reader):
    raw = await asyncio.wait_for(reader.readexactly(48), 3)
    command, sequence, _, _, _, status = struct.unpack_from("!IIIIIi", raw)
    length = struct.unpack_from("!i", raw, 24)[0] if command == 3 else 0
    data = await reader.readexactly(length)
    return command, sequence, status, data


async def _connect(port, operation=0x8003, *, busid=b"1-1\0"):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(struct.pack("!HHI", 0x111, operation, 0))
    if operation == 0x8003:
        writer.write(busid.ljust(32, b"\0"))
    await writer.drain()
    return reader, writer


def test_wire_listing_import_transfers_unlink_and_fragmented_out():
    async def scenario():
        keyboard = _Keyboard()
        bridge = UsbIpServer(keyboard, _enumeration())
        server = await asyncio.start_server(bridge._client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        writers = []
        try:
            reader, writer = await _connect(port, 0x8005)
            writers.append(writer)
            listing = await reader.read()
            assert struct.unpack_from("!HHII", listing) == (0x111, 5, 0, 1)
            assert len(listing) == 12 + 312 + 4
            assert struct.unpack_from("!HH", listing, 12 + 300) == (0x1532, 0x02A5)
            # VirtualBox can leave nonzero padding after the bus ID terminator.
            reader, writer = await _connect(port, busid=b"1-1\0" + b"X" * 28)
            writers.append(writer)
            assert struct.unpack("!HHI", await reader.readexactly(8)) == (0x111, 3, 0)
            await reader.readexactly(312)
            other_reader, other_writer = await _connect(port)
            writers.append(other_writer)
            assert struct.unpack("!HHI", await other_reader.readexactly(8)) == (0x111, 3, 1)
            writer.write(_submit(1))
            assert await _reply(reader) == (3, 1, 0, _enumeration().device)
            writer.write(_submit(2, setup=struct.pack("<BBHHH", 0x80, 6, 0x9900, 0, 18)))
            assert await _reply(reader) == (3, 2, -errno.EPIPE, b"")
            writer.write(_submit(3, endpoint=1, length=8))
            writer.write(struct.pack("!IIIIII24x", 2, 4, DEVICE_ID, 0, 0, 3))
            assert await _reply(reader) == (4, 4, -errno.ECONNRESET, b"")
            # A partially received OUT payload must not freeze virtual time.
            writer.write(
                _submit(5, direction=0, length=3, setup=struct.pack("<BBHHH", 0x21, 9, 0x300, 3, 3))
            )
            await writer.drain()
            start = keyboard.time
            for _ in range(20):
                await asyncio.sleep(0)
            assert keyboard.time > start
            writer.write(b"abc")
            raw = await reader.readexactly(48)
            assert struct.unpack_from("!ii", raw, 20) == (0, 3)
            assert keyboard.requests[-1][1] == b"abc"
            # Cancelled request 3 must never consume or receive this packet.
            keyboard.packets.append(b"hi")
            writer.write(_submit(6, endpoint=1, length=8))
            assert await _reply(reader) == (3, 6, 0, b"hi")
            keyboard.packets.append(b"")
            writer.write(_submit(7, endpoint=1, length=8, flags=1))
            assert await _reply(reader) == (3, 7, -errno.EREMOTEIO, b"")
            keyboard.packets.extend([b"12345678", b"abcdefgh", b"xy"])
            writer.write(_submit(8, endpoint=1, length=16))
            writer.write(_submit(9, endpoint=1, length=8))
            assert await _reply(reader) == (3, 8, 0, b"12345678abcdefgh")
            assert await _reply(reader) == (3, 9, 0, b"xy")
            keyboard.packets.append(b"toolong")
            writer.write(_submit(10, endpoint=1, length=2))
            assert await _reply(reader) == (3, 10, -errno.EOVERFLOW, b"to")
            writer.write(_submit(11, endpoint=1, length=0))
            assert await _reply(reader) == (3, 11, 0, b"")

        finally:
            for writer in writers:
                writer.close()
                await writer.wait_closed()
            server.close()
            await server.wait_closed()
            await asyncio.gather(*tuple(bridge._clients), return_exceptions=True)

    asyncio.run(scenario())


def test_private_firmware_control_requests_over_tcp(cpu_image):
    import os
    from functools import reduce
    from operator import xor

    import pytest

    from razer_emu import Firmware, FlashRegion
    from razer_emu.core import Keyboard

    path = os.environ.get("RAZER_TEST_FIRMWARE")
    dumps = os.environ.get("RAZER_TEST_FLASH_DUMPS")
    if not path or not dumps:
        pytest.skip("Set private firmware and verified flash captures for USB/IP integration")
    regions = [FlashRegion.load_dump(p) for p in dumps.split(os.pathsep)]
    with Keyboard(Firmware.load(path), flash_regions=regions) as keyboard:
        enumeration = keyboard.usb.enumerate_device()
        bridge = UsbIpServer(keyboard, enumeration)

        async def scenario():
            server = await asyncio.start_server(bridge._client, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            reader, writer = await _connect(port)
            try:
                assert struct.unpack("!HHI", await reader.readexactly(8)) == (0x111, 3, 0)
                await reader.readexactly(312)
                writer.write(_submit(1))
                assert await _reply(reader) == (3, 1, 0, enumeration.device)
                setup = struct.pack("<BBHHH", 0x80, 6, 0x9900, 0, 1)
                writer.write(_submit(2, length=1, setup=setup))
                assert await _reply(reader) == (3, 2, -errno.EPIPE, b"")
                setup = struct.pack("<BBHHH", 0, 5, 1, 0, 0)
                writer.write(_submit(10, direction=0, length=0, setup=setup))
                assert await _reply(reader) == (3, 10, 0, b"")
                keyboard.run_for(2000)
                setup = struct.pack("<BBHHH", 0, 9, enumeration.configuration[5], 0, 0)
                writer.write(_submit(3, direction=0, length=0, setup=setup))
                assert await _reply(reader) == (3, 3, 0, b"")
                request = bytearray(90)
                request[1], request[5], request[7] = 1, 8, 0x87
                request[88] = reduce(xor, request[2:88], 0)
                setup = struct.pack("<BBHHH", 0x21, 9, 0x300, 3, 90)
                writer.write(_submit(4, direction=0, length=90, setup=setup) + request)
                raw = await reader.readexactly(48)
                assert struct.unpack_from("!ii", raw, 20) == (0, 90)
                reply = b""
                for sequence in range(5, 15):
                    keyboard.run_for(5000)
                    setup = struct.pack("<BBHHH", 0xA1, 1, 0x300, 3, 90)
                    writer.write(_submit(sequence, length=90, setup=setup))
                    _, returned_sequence, status, reply = await _reply(reader)
                    assert returned_sequence == sequence and status == 0
                    if reply[0] == 2:
                        break
                assert reply[0] == 2 and reply[6:11] == b"\x00\x87\x01\x01\x01"
                assert reduce(xor, reply[2:88], 0) == reply[88]
                writer.write(_submit(20, endpoint=1, length=8))
                writer.write(struct.pack("!IIIIII24x", 2, 21, DEVICE_ID, 0, 0, 20))
                assert await _reply(reader) == (4, 21, -errno.ECONNRESET, b"")
                assert keyboard.state().stopped_reason is None
            finally:
                writer.close()
                await writer.wait_closed()
                server.close()
                await server.wait_closed()
                await asyncio.gather(*tuple(bridge._clients), return_exceptions=True)

        asyncio.run(scenario())
