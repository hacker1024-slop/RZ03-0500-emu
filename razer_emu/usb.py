"""Bounded USB host transactions against firmware-backed virtual endpoints."""

import struct
from dataclasses import dataclass


class UsbError(RuntimeError):
    """The virtual device or its emulation could not complete a transaction."""


class UsbTimeout(UsbError):
    """The virtual-time transaction budget expired."""


class UsbStall(UsbError):
    """Firmware stalled an endpoint."""


@dataclass(frozen=True)
class SetupPacket:
    request_type: int
    request: int
    value: int = 0
    index: int = 0
    length: int = 0

    def __post_init__(self):
        for name, limit in (
            ("request_type", 255),
            ("request", 255),
            ("value", 65535),
            ("index", 65535),
            ("length", 65535),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= limit:
                raise ValueError(f"Invalid USB {name}")

    def pack(self):
        return struct.pack(
            "<BBHHH", self.request_type, self.request, self.value, self.index, self.length
        )


@dataclass(frozen=True)
class UsbEnumeration:
    device: bytes
    configuration: bytes

    def __post_init__(self):
        if len(self.device) != 18 or self.device[:2] != b"\x12\x01":
            raise UsbError("Invalid device descriptor")
        if len(self.configuration) < 9 or self.configuration[:2] != b"\x09\x02":
            raise UsbError("Invalid configuration descriptor")
        if struct.unpack_from("<H", self.configuration, 2)[0] != len(self.configuration):
            raise UsbError("Configuration descriptor length mismatch")
        if not self.configuration[5]:
            raise UsbError("Configuration value cannot be zero")

    @property
    def interrupt_inputs(self):
        """(address, packet size, interval_us) for high-speed interrupt IN endpoints."""
        result = []
        offset = 0
        while offset < len(self.configuration):
            item = self.configuration[offset:]
            if len(item) < 2 or item[0] < 2 or item[0] > len(item):
                raise UsbError("Malformed configuration descriptor")
            if item[1] == 5 and item[0] >= 7:
                address, attributes, size, interval = struct.unpack_from("<BBHB", item, 2)
                if attributes & 3 == 3 and address & 0x80:
                    if not 1 <= size <= 1024 or not 1 <= interval <= 16:
                        raise UsbError("Unsupported high-speed interrupt endpoint")
                    result.append((address, size, 125 << (interval - 1)))
            offset += item[0]
        return tuple(result)

    def to_dict(self):
        vid, pid = struct.unpack_from("<HH", self.device, 8)
        return {
            "vid": f"0x{vid:04x}",
            "pid": f"0x{pid:04x}",
            "interfaces": self.configuration[4],
            "configuration": self.configuration[5],
            "device_descriptor": self.device.hex(),
            "configuration_descriptor": self.configuration.hex(),
        }


class UsbHost:
    """Synchronous host on the Keyboard's owning thread; uses only virtual time.

    Firmware supplies all descriptors, class responses and endpoint buffers.
    These methods do not open a physical USB device or start a network server.
    """

    def __init__(self, keyboard, backend):
        self._keyboard = keyboard
        self._backend = backend
        self._interrupt_due = {}

    def _reset_timing(self):
        self._interrupt_due.clear()

    @property
    def connected(self):
        self._keyboard.state()  # reject access after Keyboard.close()
        return bool(self._backend.Connected)

    @property
    def address(self):
        self._keyboard.state()
        return int(self._backend.Address)

    def connect(self, *, timeout_us=100_000):
        """Apply host power and run firmware until it requests USB connection."""
        deadline = self._deadline(timeout_us)
        self._backend.VbusPresent = True
        while not self._backend.Connected:
            self._advance(deadline)

    def disconnect(self):
        """Remove host power; firmware handles the event during subsequent runs."""
        self._keyboard.state()
        self._backend.VbusPresent = False
        self._reset_timing()

    def reset_bus(self, *, high_speed=True, timeout_us=20_000):
        """Reset USB protocol state and allow 10 ms of virtual recovery time."""
        deadline = self._deadline(timeout_us)
        if type(high_speed) is not bool:
            raise ValueError("high_speed must be a boolean")
        if timeout_us < 10_000:
            raise ValueError("Bus reset requires at least 10,000 us of recovery budget")
        while not self._backend.HostReset(high_speed):
            self._advance(deadline)
        self._reset_timing()
        remaining = deadline - self._check().elapsed_us
        if remaining < 10_000:
            raise UsbTimeout("Insufficient virtual time for USB reset recovery")
        self._keyboard.run_for(10_000)
        self._check()

    def enumerate_device(self):
        """Attach, reset, assign address 1, read and select the first configuration."""
        self.connect()
        self.reset_bus()
        device = self.control(SetupPacket(0x80, 6, value=0x100, length=18))
        if len(device) != 18 or device[:2] != b"\x12\x01" or device[17] == 0:
            raise UsbError("Device did not provide a usable device descriptor")
        self.control(SetupPacket(0, 5, value=1))
        self._keyboard.run_for(2_000)  # address recovery, in virtual time
        self._check()
        header = self.control(SetupPacket(0x80, 6, value=0x200, length=9))
        if len(header) != 9 or header[:2] != b"\x09\x02":
            raise UsbError("Device did not provide a configuration header")
        length = struct.unpack_from("<H", header, 2)[0]
        if length < 9:
            raise UsbError("Configuration descriptor is too short")
        configuration = self.control(SetupPacket(0x80, 6, value=0x200, length=length))
        result = UsbEnumeration(device, configuration)
        self.control(SetupPacket(0, 9, value=configuration[5]))
        return result

    def control(self, setup: SetupPacket, data=b"", *, timeout_us=100_000):
        """Execute SETUP, optional data, and status stages on endpoint zero.

        Returns the received bytes for IN requests, or b'' for OUT requests.
        Short IN responses end the data stage. NAKs advance virtual time; STALL
        and emulator stops are reported separately from deadline expiry.
        """
        if not isinstance(setup, SetupPacket):
            raise TypeError("Expected a SetupPacket")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("Expected a byte buffer")
        data = bytes(data)
        is_read = bool(setup.request_type & 0x80)
        if (is_read and data) or (not is_read and len(data) != setup.length):
            raise ValueError("USB OUT payload must match wLength; IN requests have no OUT payload")
        deadline = self._deadline(timeout_us)
        self._packet(lambda: self._backend.HostSetup(self._array(setup.pack())), deadline)
        result = bytearray()
        if is_read and setup.length:
            while len(result) < setup.length:
                packet = self._packet(lambda: self._backend.HostIn(0, 64), deadline, read=True)
                result.extend(packet)
                if len(result) > setup.length:
                    raise UsbError("Device returned more data than wLength")
                if len(packet) < 64:
                    break
            self._packet(lambda: self._backend.HostOut(0, self._array(b""), 64), deadline)
        else:
            for offset in range(0, len(data), 64):
                packet = self._array(data[offset : offset + 64])
                self._packet(lambda: self._backend.HostOut(0, packet, 64), deadline)
            status = self._packet(lambda: self._backend.HostIn(0, 64), deadline, read=True)
            if status:
                raise UsbError("Control status stage contained data")
        # Give firmware a service quantum for the final completion interrupt.
        if self._check().elapsed_us < deadline:
            self._advance(deadline)
        return bytes(result)

    def read_interrupt(self, endpoint, maximum_packet_size, *, interval_us=125, timeout_us=100_000):
        """Receive one IN packet, polling no faster than the supplied interval.

        Endpoint is the USB address (0x81 through 0x85). Pass packet size and
        interval from the firmware's descriptor. A short or zero-length ACK is
        returned unchanged. Completion IRQ service occurs when virtual time next
        advances. Endpoint polling cadence persists across calls.
        """
        self._validate_interrupt(endpoint, maximum_packet_size, interval_us)
        deadline = self._deadline(timeout_us)
        while True:
            packet = self.poll_interrupt(endpoint, maximum_packet_size, interval_us=interval_us)
            if packet is not None:
                return packet
            now = self._check().elapsed_us
            due = self._interrupt_due[endpoint]
            self._advance(min(due, deadline), due - now)
            if self._check().elapsed_us < due:
                raise UsbTimeout("Next interrupt poll falls outside the virtual-time budget")

    def poll_interrupt(self, endpoint, maximum_packet_size, *, interval_us=125):
        """Attempt one interrupt IN transaction without advancing virtual time.

        Return None on NAK or before the endpoint's next polling slot; return
        bytes on ACK, including b'' for a zero-length packet. The caller advances
        virtual time and services completion interrupts with Keyboard.run_for().
        Poll slots are shared with read_interrupt(). STALL and emulator errors
        raise the same exceptions as blocking transfers.
        """
        self._validate_interrupt(endpoint, maximum_packet_size, interval_us)
        now = self._check().elapsed_us
        if not self._backend.Connected:
            raise UsbError("Virtual USB device is disconnected")
        if now < self._interrupt_due.get(endpoint, now):
            return None
        self._interrupt_due[endpoint] = now + interval_us
        result = self._backend.HostIn(endpoint & 15, maximum_packet_size)
        handshake = str(result.Handshake)
        if handshake == "Ack":
            return bytes(result.Data)
        if handshake == "Stall":
            raise UsbStall(f"Firmware stalled endpoint 0x{endpoint:02x}")
        return None

    @staticmethod
    def _validate_interrupt(endpoint, maximum_packet_size, interval_us):
        if type(endpoint) is not int or not 0x81 <= endpoint <= 0x85:
            raise ValueError("Interrupt endpoint must be an IN address from 0x81 to 0x85")
        if type(maximum_packet_size) is not int or not 1 <= maximum_packet_size <= 1024:
            raise ValueError("Maximum packet size must be 1 to 1024 bytes")
        if type(interval_us) is not int or not 1 <= interval_us <= 10_000_000:
            raise ValueError("Polling interval must be 1 to 10,000,000 virtual microseconds")

    @staticmethod
    def _array(data):
        from System import Array, Byte

        return Array[Byte](data)

    def _check(self):
        state = self._keyboard.state()
        if state.stopped_reason:
            raise UsbError(f"Emulation stopped: {state.stopped_reason}")
        return state

    def _deadline(self, timeout_us):
        if type(timeout_us) is not int or not 1 <= timeout_us <= 10_000_000:
            raise ValueError("USB timeout must be 1 to 10,000,000 virtual microseconds")
        return self._check().elapsed_us + timeout_us

    def _advance(self, deadline, quantum_us=10):
        remaining = deadline - self._check().elapsed_us
        if remaining <= 0:
            raise UsbTimeout("USB transaction exceeded its virtual-time budget")
        self._keyboard.run_for(min(quantum_us, remaining))
        self._check()

    def _packet(self, operation, deadline, *, read=False, endpoint=0, poll_interval_us=10):
        while True:
            self._check()
            if not self._backend.Connected:
                raise UsbError("Virtual USB device is disconnected")
            result = operation()
            handshake = str(result.Handshake if read else result)
            if handshake == "Ack":
                return bytes(result.Data) if read else None
            if handshake == "Stall":
                raise UsbStall(f"Firmware stalled endpoint 0x{endpoint:02x}")
            self._advance(deadline, poll_interval_us)
