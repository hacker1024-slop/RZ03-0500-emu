"""USB/IP transport for firmware-backed control and interrupt IN endpoints.

The asyncio loop and Keyboard must share their owning thread. No worker thread
executes firmware. USB/IP framing follows docs.kernel.org/usb/usbip_protocol.html.
"""

import asyncio
import errno
import logging
import struct
import time
from dataclasses import dataclass

from .usb import SetupPacket, UsbError, UsbStall, UsbTimeout

LOG = logging.getLogger(__name__)
VERSION = 0x0111
BUSID = b"1-1"
DEVICE_ID = 0x10001
MAX_TRANSFER = 65536


@dataclass
class _Pending:
    endpoint: int
    length: int
    flags: int
    data: bytearray


class UsbIpServer:
    """Export one emulated keyboard to one importing USB/IP client at a time.

    Construct after firmware enumeration. serve() defaults to loopback; this
    protocol has no authentication. Only control and interrupt IN are supported.
    All returned USB data originate in firmware.
    """

    def __init__(self, keyboard, enumeration, *, pace=None):
        self.keyboard = keyboard
        self.host = keyboard.usb
        self._pace = pace
        self.enumeration = enumeration
        self.interfaces = []
        self.endpoints = {}
        configuration = enumeration.configuration
        offset = 0
        while offset < len(configuration):
            if len(configuration) - offset < 2:
                raise ValueError("Truncated configuration descriptor")
            length, kind = configuration[offset : offset + 2]
            if length < 2 or offset + length > len(configuration):
                raise ValueError("Malformed configuration descriptor")
            item = configuration[offset : offset + length]
            if kind == 4 and length >= 9 and item[3] == 0:
                self.interfaces.append(item[5:8] + b"\0")
            if kind == 5 and length >= 7:
                address, attributes, size, interval = struct.unpack_from(
                    "<BBHB", item, 2
                )
                if attributes & 3 == 3 and address & 0x80:
                    if not 1 <= interval <= 16 or not 1 <= size <= 1024:
                        raise ValueError("Unsupported high-speed interrupt descriptor")
                    self.endpoints[address & 15] = (size, 125 << (interval - 1))
            offset += length
        if len(self.interfaces) != configuration[4]:
            raise ValueError("Configuration interface count mismatch")
        self._imported = False
        self._clients = {}

    def _device_record(self):
        device = self.enumeration.device
        vid, pid, revision = struct.unpack_from("<HHH", device, 8)
        return struct.pack(
            "!256s32sIIIHHHBBBBBB",
            b"/razer-emu/usb/1-1",
            BUSID,
            1,
            1,
            3,
            vid,
            pid,
            revision,
            *device[4:7],
            self.enumeration.configuration[5],
            device[17],
            len(self.interfaces),
        )

    @property
    def imported(self):
        """Whether a guest currently owns the exported device."""
        return self._imported

    async def serve(self, address="127.0.0.1", port=3240):
        """Serve until cancelled; close clients before returning."""
        def accept(reader, writer):
            task = asyncio.create_task(self._client(reader, writer))
            self._clients[task] = writer
            task.add_done_callback(lambda finished: self._clients.pop(finished, None))

        server = await asyncio.start_server(accept, address, port, limit=MAX_TRANSFER + 48)
        try:
            # Close guest transports before waiting for server shutdown.
            await asyncio.Future()
        finally:
            server.close()
            clients = tuple(self._clients.items())
            for task, writer in clients:
                writer.close()
                task.cancel()
            await asyncio.gather(*(task for task, _ in clients), return_exceptions=True)
            await server.wait_closed()

    async def _client(self, reader, writer):
        task = asyncio.current_task()
        self._clients[task] = writer
        imported = False
        try:
            version, operation, status = struct.unpack(
                "!HHI", await reader.readexactly(8)
            )
            if version != VERSION or status:
                return
            if operation == 0x8005:
                writer.write(struct.pack("!HHII", VERSION, 5, 0, 1))
                writer.write(self._device_record() + b"".join(self.interfaces))
            elif operation == 0x8003:
                # Clients may leave bytes after the C string terminator unset.
                busid = (await reader.readexactly(32)).split(b"\0", 1)[0]
                if busid != BUSID or self._imported:
                    writer.write(struct.pack("!HHI", VERSION, 3, 1))
                else:
                    self._imported = imported = True
                    if self._pace is not None:
                        started = time.monotonic()
                        before = self.keyboard.state().elapsed_us
                    self.host.reset_bus()
                    # VHCI handles SET_ADDRESS locally: the exported device
                    # must already have its own server-side USB address.
                    self.host.control(SetupPacket(0, 5, value=1))
                    self.keyboard.run_for(2000)
                    if self._pace is not None:
                        await self._pace(started, before, self.keyboard.state().elapsed_us)
                    writer.write(
                        struct.pack("!HHI", VERSION, 3, 0) + self._device_record()
                    )
                    await writer.drain()
                    await self._transfers(reader, writer)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except (ValueError, UsbError) as exc:
            LOG.warning("USB/IP connection closed: %s", exc)
        finally:
            if imported:
                self._imported = False
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            finally:
                self._clients.pop(task, None)

    @staticmethod
    def _reply(writer, sequence, status=0, data=b"", *, actual=None, unlink=False):
        if unlink:
            writer.write(struct.pack("!IIIIIi24x", 4, sequence, 0, 0, 0, status))
        else:
            writer.write(
                struct.pack(
                    "!IIIIIiiiii8x",
                    3,
                    sequence,
                    0,
                    0,
                    0,
                    status,
                    len(data) if actual is None else actual,
                    0,
                    -1,
                    0,
                )
                + data
            )

    @staticmethod
    async def _read_request(reader):
        raw = await reader.readexactly(48)
        command, _, _, direction, _ = struct.unpack_from("!IIIII", raw)
        payload = b""
        if command == 1:
            length = struct.unpack_from("!i", raw, 24)[0]
            if not 0 <= length <= MAX_TRANSFER:
                raise ValueError("Unsupported USB/IP transfer length")
            if direction == 0:
                payload = await reader.readexactly(length)
        return raw, payload

    async def _transfers(self, reader, writer):
        pending = {}
        pump = None
        if hasattr(self.keyboard, "_runner"):
            from Antmicro.Renode.Peripherals.USB import RazerUsbInterruptPump

            pump = RazerUsbInterruptPump(self.keyboard._usb_device)
        receive = asyncio.create_task(self._read_request(reader))
        try:
            while True:
                if self._pace is not None:
                    started = time.monotonic()
                    before = self.keyboard.state().elapsed_us
                if receive.done():
                    raw, payload = receive.result()
                    command, sequence, device, direction, endpoint = struct.unpack_from(
                        "!IIIII", raw
                    )
                    if command == 2:
                        target = struct.unpack_from("!I", raw, 20)[0]
                        removed = pending.pop(target, None)
                        if pump is not None and removed is not None:
                            removed = pump.Cancel(target)
                        self._reply(
                            writer,
                            sequence,
                            -errno.ECONNRESET if removed else 0,
                            unlink=True,
                        )
                    elif command == 1:
                        flags, length, _, packets, _ = struct.unpack_from(
                            "!Iiiii", raw, 20
                        )
                        if not 0 <= length <= MAX_TRANSFER or packets not in (0, -1):
                            raise ValueError(
                                "Unsupported USB/IP transfer length or isochronous request"
                            )
                        if direction not in (0, 1) or device != DEVICE_ID:
                            raise ValueError("Invalid USB/IP device or direction")
                        if sequence in pending:
                            raise ValueError("Duplicate pending USB/IP sequence")
                        if endpoint == 0:
                            setup = SetupPacket(*struct.unpack("<BBHHH", raw[40:48]))
                            if setup.length > length or (
                                setup.length
                                and bool(direction) != bool(setup.request_type & 128)
                            ):
                                raise ValueError(
                                    "USB/IP setup and transfer length/direction disagree"
                                )
                            try:
                                data = self.host.control(setup, payload)
                                self._reply(
                                    writer,
                                    sequence,
                                    data=data,
                                    actual=len(data) if direction else len(payload),
                                )
                            except UsbStall:
                                self._reply(writer, sequence, -errno.EPIPE)
                            except UsbTimeout:
                                self._reply(writer, sequence, -errno.ETIMEDOUT)
                        elif direction == 1 and endpoint in self.endpoints:
                            if length == 0:
                                self._reply(writer, sequence)
                                receive = asyncio.create_task(
                                    self._read_request(reader)
                                )
                                continue
                            if len(pending) >= 128:
                                raise ValueError("Too many pending USB/IP transfers")
                            if pump is None:
                                pending[sequence] = _Pending(
                                    endpoint, length, flags, bytearray()
                                )
                            else:
                                size, interval = self.endpoints[endpoint]
                                pump.Add(
                                    sequence, endpoint, length, flags, size, interval
                                )
                                pending[sequence] = True
                        else:
                            self._reply(writer, sequence, -errno.EPIPE)
                    else:
                        raise ValueError("Unsupported USB/IP command")
                    receive = asyncio.create_task(self._read_request(reader))
                if pump is None:
                    # Queue order matters when several URBs target the same endpoint.
                    visited = set()
                    for sequence, request in tuple(pending.items()):
                        if request.endpoint in visited:
                            continue
                        visited.add(request.endpoint)
                        size, interval = self.endpoints[request.endpoint]
                        try:
                            packet = self.host.poll_interrupt(
                                request.endpoint | 128, size, interval_us=interval
                            )
                        except UsbStall:
                            self._reply(writer, sequence, -errno.EPIPE)
                            del pending[sequence]
                            continue
                        if packet is None:
                            continue
                        remaining = request.length - len(request.data)
                        request.data.extend(packet[:remaining])
                        short = len(packet) < size
                        if (
                            len(packet) > remaining
                            or short
                            or len(request.data) == request.length
                        ):
                            status = -errno.EOVERFLOW if len(packet) > remaining else 0
                            if (
                                short
                                and request.flags & 1
                                and len(request.data) < request.length
                            ):
                                status = -errno.EREMOTEIO
                            self._reply(writer, sequence, status, bytes(request.data))
                            del pending[sequence]
                else:
                    state = self.keyboard.run_for(5000, _usb_pump=pump)
                    if state.stopped_reason:
                        raise UsbError(f"Emulation stopped: {state.stopped_reason}")
                    for completion in pump.Drain():
                        sequence = int(completion.Sequence)
                        pending.pop(sequence, None)
                        self._reply(
                            writer,
                            sequence,
                            int(completion.Status),
                            bytes(completion.Data),
                        )
                await writer.drain()
                if pump is None:
                    self.keyboard.run_for(125)
                    self.host._check()
                if pump is not None and not self.keyboard._usb_device.Connected:
                    raise UsbError("Virtual USB device is disconnected")
                if self._pace is not None:
                    await self._pace(started, before, self.keyboard.state().elapsed_us)
                # Yield to sockets and cancellation even with continuous NAKs.
                await asyncio.sleep(0)
        finally:
            receive.cancel()
            await asyncio.gather(receive, return_exceptions=True)
