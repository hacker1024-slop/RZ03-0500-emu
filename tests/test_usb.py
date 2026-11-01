"""IP3511 packet DMA and handshake integration, without proprietary images."""

import struct

import pytest

from razer_emu import Firmware
from razer_emu.core import Keyboard

BASE = 0x40094000
TABLE = 0x40100100
BUFFER = 0x40100400


def _array(data):
    from System import Array, Byte

    return Array[Byte](data)


def _arm(bus, index, data_address, count, *, bank=0, flags=0x80000000):
    command = flags | (count << 11) | ((data_address >> 6) & 0x7FF)
    bus.WriteDoubleWord(TABLE + index * 8 + bank * 4, command)
    return command


@pytest.fixture
def usb_board(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus = keyboard._bus
        device = keyboard._machine.sysbus.usbDevice.internal
        # Synthetic firmware configuration. Only the CPU side writes DCON.
        bus.WriteDoubleWord(0x40038038, 0xC0000000)
        bus.WriteDoubleWord(0x40038000, 0)
        bus.WriteDoubleWord(0x400380A4, 0x203040)
        device.VbusPresent = True
        bus.WriteDoubleWord(BASE, 0x08010080)
        bus.WriteDoubleWord(BASE + 8, TABLE)
        bus.WriteDoubleWord(TABLE + 4, (BUFFER >> 6) & 0x7FF)
        assert device.HostReset(True)
        bus.WriteDoubleWord(BASE, device.ReadDoubleWord(0) | 0x0F000100)
        bus.WriteDoubleWord(BASE + 0x20, 0xC0000FFF)
        yield keyboard, bus, device


def test_bus_reset_resets_protocol_without_resetting_cpu_or_ram(usb_board):
    keyboard, bus, device = usb_board
    bus.WriteDoubleWord(BASE, device.ReadDoubleWord(0) | 42)
    bus.WriteDoubleWord(BASE + 0x24, 0x80000000)
    bus.WriteDoubleWord(BUFFER, 0xDEADBEEF)
    pc = keyboard.state().pc
    assert device.Address == 42
    assert device.HostReset(False)
    assert device.Address == 0
    assert device.ReadDoubleWord(0) & 0xC00000 == 0x400000
    assert device.ReadDoubleWord(0) & 0x04000000
    assert device.IRQ.IsSet
    assert keyboard.state().pc == pc
    assert keyboard.read_memory(BUFFER, 4) == struct.pack("<I", 0xDEADBEEF)
    device.VbusPresent = False
    assert not device.HostReset(True)


def test_host_frames_drive_sof_interrupts_and_frame_number(usb_board):
    keyboard, bus, device = usb_board
    bus.WriteDoubleWord(BASE + 0x24, 0x40000000)
    keyboard.run_for(124)
    assert not device.IRQ.IsSet
    keyboard.run_for(1)
    assert device.IRQ.IsSet
    assert device.ReadDoubleWord(4) & 0x7FF == 0
    bus.WriteDoubleWord(BASE + 0x20, 0x40000000)
    assert not device.IRQ.IsSet
    keyboard.run_for(875)
    assert device.ReadDoubleWord(4) & 0x7FF == 1
    assert device.IRQ.IsSet
    assert device.HostReset(False)
    bus.WriteDoubleWord(BASE + 0x20, 0xC0000FFF)
    keyboard.run_for(999)
    assert not device.IRQ.IsSet
    keyboard.run_for(1)
    assert device.IRQ.IsSet
    assert device.ReadDoubleWord(4) & 0x7FF == 1
    device.VbusPresent = False
    keyboard.run_for(2000)
    assert device.ReadDoubleWord(4) & 0x7FF == 1


def test_setup_aborts_control_stalls_and_out_zero_length_packet(usb_board):
    keyboard, bus, device = usb_board
    _arm(bus, 0, BUFFER + 64, 64, flags=0xA0000000)
    _arm(bus, 1, BUFFER + 128, 64, flags=0xA0000000)
    packet = struct.pack("<BBHHH", 0x80, 6, 0x100, 0, 18)
    assert str(device.HostSetup(_array(packet))) == "Ack"
    assert keyboard.read_memory(BUFFER, 8) == packet
    assert bus.ReadDoubleWord(TABLE) & 0xA0000000 == 0
    assert bus.ReadDoubleWord(TABLE + 8) & 0xA0000000 == 0
    assert device.ReadDoubleWord(0) & 0x100
    assert device.ReadDoubleWord(0x20) & 3 == 1
    assert device.ReadDoubleWord(0x34) & 3 == 3
    assert str(device.HostOut(0, _array(b""), 64)) == "Nak"
    bus.WriteDoubleWord(BASE, device.ReadDoubleWord(0) | 0x100)
    _arm(bus, 0, BUFFER + 64, 0)
    bus.WriteDoubleWord(BUFFER + 64, 0xFFFFFFFF)
    assert str(device.HostOut(0, _array(b""), 64)) == "Ack"
    assert keyboard.read_memory(BUFFER + 64, 4) == bytes(4)
    assert bus.ReadDoubleWord(TABLE) & 0x80000000 == 0


def test_in_packets_advance_buffer_and_complete_only_after_all_bytes(usb_board):
    keyboard, bus, device = usb_board
    payload = bytes(range(90))
    bus.WriteBytes(_array(payload), BUFFER)
    # TYPE+RFTV is the NXP HS interrupt workaround, not an isochronous request.
    _arm(bus, 3, BUFFER, len(payload), flags=0x8C000000)
    first = device.HostIn(1, 64)
    assert str(first.Handshake) == "Ack" and bytes(first.Data) == payload[:64]
    command = bus.ReadDoubleWord(TABLE + 24)
    assert (command >> 11) & 0x7FFF == 26 and command & 0x80000000
    assert device.ReadDoubleWord(0x20) & 8 == 0
    second = device.HostIn(1, 64)
    assert str(second.Handshake) == "Ack" and bytes(second.Data) == payload[64:]
    assert bus.ReadDoubleWord(TABLE + 24) & 0x80000000 == 0
    assert device.ReadDoubleWord(0x20) & 8
    assert str(device.HostIn(1, 64).Handshake) == "Nak"
    assert keyboard.state().stopped_reason is None


def test_out_short_packet_preserves_remaining_count_for_firmware(usb_board):
    keyboard, bus, device = usb_board
    _arm(bus, 2, BUFFER, 128)
    assert str(device.HostOut(1, _array(bytes(range(64))), 64)) == "Ack"
    assert device.ReadDoubleWord(0x20) & 4 == 0
    assert str(device.HostOut(1, _array(b"abcdefg"), 64)) == "Ack"
    command = bus.ReadDoubleWord(TABLE + 16)
    assert command & 0x80000000 == 0
    assert (command >> 11) & 0x7FFF == 57
    assert keyboard.read_memory(BUFFER, 71) == bytes(range(64)) + b"abcdefg"
    assert device.ReadDoubleWord(0x20) & 4


def test_double_buffer_selection_and_abort_reprime_dma_position(usb_board):
    keyboard, bus, device = usb_board
    bus.WriteDoubleWord(BASE + 0x1C, 8)
    bus.WriteBytes(_array(b"abcdEFGH"), BUFFER)
    bus.WriteBytes(_array(b"ijkl"), BUFFER + 64)
    _arm(bus, 3, BUFFER, 4)
    _arm(bus, 3, BUFFER + 64, 4, bank=1)
    assert bytes(device.HostIn(1, 64).Data) == b"abcd"
    assert device.ReadDoubleWord(0x18) & 8
    assert bytes(device.HostIn(1, 64).Data) == b"ijkl"
    assert device.ReadDoubleWord(0x18) & 8 == 0
    # Cancel after partial progress, then re-prime with the same remaining count.
    _arm(bus, 3, BUFFER, 8)
    assert bytes(device.HostIn(1, 4).Data) == b"abcd"
    bus.WriteDoubleWord(BASE + 0x14, 8)
    _arm(bus, 3, BUFFER, 4)
    assert bytes(device.HostIn(1, 64).Data) == b"abcd"
    assert keyboard.state().stopped_reason is None


def test_inactive_disabled_stalled_and_invalid_dma_are_distinct(usb_board):
    keyboard, bus, device = usb_board
    assert str(device.HostIn(1, 64).Handshake) == "Nak"
    _arm(bus, 3, BUFFER, 8, flags=0x20000000)
    assert str(device.HostIn(1, 64).Handshake) == "Stall"
    _arm(bus, 3, BUFFER, 8, flags=0x60000000)
    assert str(device.HostIn(1, 64).Handshake) == "Nak"
    _arm(bus, 3, 0x40104000, 8)
    assert str(device.HostIn(1, 64).Handshake) == "Nak"
    assert keyboard.state().stopped_reason == "unsupported_usb_transfer"
    assert keyboard.state().unknown_reads == keyboard.state().unknown_writes == 0
    assert any("DMA outside USB RAM" in line for line in keyboard.logs())


def test_host_core_timeout_is_virtual_and_close_invalidates_connection(usb_board):
    keyboard, _, _ = usb_board
    from razer_emu import SetupPacket, UsbError, UsbTimeout

    start = keyboard.state().elapsed_us
    # This synthetic CPU does not service USB requests: the control IN stays NAK.
    with pytest.raises(UsbTimeout):
        keyboard.usb.control(SetupPacket(0x80, 6, value=0x100, length=18), timeout_us=100)
    assert keyboard.state().elapsed_us - start == 100
    keyboard.usb.disconnect()
    with pytest.raises(UsbError, match="disconnected"):
        keyboard.usb.control(SetupPacket(0x80, 6, value=0x100, length=18))
    keyboard.close()
    with pytest.raises(RuntimeError, match="closed"):
        _ = keyboard.usb.connected


def test_private_firmware_enumerates_and_handles_razer_feature_reports(cpu_image):
    import os
    from functools import reduce
    from operator import xor

    from razer_emu import FlashRegion, SetupPacket, UsbStall

    path = os.environ.get("RAZER_TEST_FIRMWARE")
    dumps = os.environ.get("RAZER_TEST_FLASH_DUMPS")
    if not path or not dumps:
        pytest.skip("Set private firmware and verified flash captures for USB integration")
    regions = [FlashRegion.load_dump(p) for p in dumps.split(os.pathsep)]
    with Keyboard(Firmware.load(path), flash_regions=regions) as keyboard:
        host = keyboard.usb
        enumeration = host.enumerate_device()
        assert struct.unpack_from("<HH", enumeration.device, 8) == (0x1532, 0x02A5)
        assert host.address == 1
        config = enumeration.configuration
        assert config[4] == 5 and len(config) == 134
        # A short response terminates the read, even when wLength requests 64.
        assert host.control(SetupPacket(0x80, 6, value=0x100, length=64)) == enumeration.device
        with pytest.raises(UsbStall):
            host.control(SetupPacket(0x80, 6, value=0x9900, length=1))
        assert host.control(SetupPacket(0x80, 6, value=0x100, length=18)) == enumeration.device
        # Read every HID report descriptor through the original interface handlers.
        offset = interface = 0
        lengths = {}
        while offset < len(config):
            size = config[offset]
            assert size >= 2
            descriptor = config[offset : offset + size]
            if descriptor[1] == 4:
                interface = descriptor[2]
            if descriptor[1] == 0x21:
                length = struct.unpack_from("<H", descriptor, 7)[0]
                report = host.control(
                    SetupPacket(0x81, 6, value=0x2200, index=interface, length=length)
                )
                assert len(report) == length
                lengths[interface] = length
            offset += size
        assert lengths == {0: 61, 1: 177, 2: 88, 3: 22, 4: 327}
        # Actual 90-byte Razer feature traffic, spanning multiple USB packets.
        # Query 00/87 returns the installed firmware version; no host-side reply
        # implementation or proprietary binary fixture is supplied by this test.
        request = bytearray(90)
        request[1], request[5], request[7] = 1, 8, 0x87
        request[88] = reduce(xor, request[2:88], 0)
        host.control(SetupPacket(0x21, 9, value=0x300, index=3, length=90), request)
        for _ in range(10):
            keyboard.run_for(5000)
            reply = host.control(SetupPacket(0xA1, 1, value=0x300, index=3, length=90))
            if reply[0] == 2:
                break
        assert len(reply) == 90 and reply[0] == 2
        assert reply[1] == 1 and reply[6:8] == b"\x00\x87"
        assert reply[8:11] == b"\x01\x01\x01"
        assert reduce(xor, reply[2:88], 0) == reply[88]
        assert keyboard.state().stopped_reason is None
        # Exercise the later I2C DMA attempt and its NACK/abort path too.
        assert keyboard.run_for(120_000).stopped_reason is None
        assert keyboard._bus.ReadDoubleWord(0x400824D0) & 1
        assert keyboard._bus.ReadDoubleWord(0x40082058) == 0
