"""DMA integration using CPU-visible descriptors and peripheral request lines."""

import struct

from razer_emu import Firmware
from razer_emu.core import Keyboard

BASE = 0x40082000
TABLE = 0x20028000
SOURCE = TABLE + 0x400
TARGET = TABLE + 0x800


def _words(bus, address, *words):
    for index, word in enumerate(words):
        bus.WriteDoubleWord(address + index * 4, word)


def test_memory_copy_chaining_widths_and_completion_interrupt(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus = keyboard._bus
        dma = keyboard._machine.sysbus.dma0.internal
        _words(bus, SOURCE, 0x44332211, 0x88776655)
        # Two byte beats, then two halfword beats. Descriptors use end addresses.
        first = 0x10000 | 0x5000 | 0x17
        second = 0x10000 | 0x5000 | 0x100 | 0x29
        _words(bus, TABLE, first, SOURCE + 1, TARGET + 1, TABLE + 16)
        _words(bus, TABLE + 16, second, SOURCE + 6, TARGET + 6, 0)
        bus.WriteDoubleWord(BASE + 8, TABLE)
        bus.WriteDoubleWord(BASE + 0x48, 1)
        bus.WriteDoubleWord(BASE + 0x20, 1)
        bus.WriteDoubleWord(BASE, 1)
        bus.WriteDoubleWord(BASE + 0x408, first)
        assert keyboard.run_for(4).stopped_reason is None
        assert keyboard.read_memory(TARGET, 8) == bytes.fromhex("1122000055667788")
        assert bus.ReadDoubleWord(BASE + 0x58) == 1
        assert bus.ReadDoubleWord(BASE + 0x60) == 1
        assert bus.ReadDoubleWord(BASE + 0x408) & 1 == 0
        assert bus.ReadDoubleWord(BASE + 0x404) & 4 == 0
        assert dma.IRQ.IsSet
        bus.WriteDoubleWord(BASE + 0x58, 1)
        assert dma.IRQ.IsSet
        bus.WriteDoubleWord(BASE + 0x60, 1)
        assert not dma.IRQ.IsSet


def test_peripheral_request_gates_each_beat_and_abort_cancels(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus = keyboard._bus
        dma = keyboard._machine.sysbus.dma0.internal
        _words(bus, SOURCE, 0x44332211)
        _words(bus, TABLE, 0, SOURCE + 3, TARGET + 3, 0)
        bus.WriteDoubleWord(BASE + 8, TABLE)
        bus.WriteDoubleWord(BASE, 1)
        bus.WriteDoubleWord(BASE + 0x20, 1)
        bus.WriteDoubleWord(BASE + 0x400, 1)
        bus.WriteDoubleWord(BASE + 0x408, 0x35015)
        keyboard.run_for(10)
        assert keyboard.read_memory(TARGET, 4) == bytes(4)
        assert not dma.IRQ.IsSet
        dma.OnGPIO(0, True)
        keyboard.run_for(1)
        dma.OnGPIO(0, False)
        keyboard.run_for(10)
        assert keyboard.read_memory(TARGET, 4) == b"\x11\0\0\0"
        bus.WriteDoubleWord(BASE + 0x78, 1)
        assert bus.ReadDoubleWord(BASE + 0x78) == 0
        dma.OnGPIO(0, True)
        keyboard.run_for(10)
        assert keyboard.read_memory(TARGET, 4) == b"\x11\0\0\0"
        assert bus.ReadDoubleWord(BASE + 0x58) == 0
        assert keyboard.state().stopped_reason is None


def test_i2c_address_nack_never_generates_dma_completion(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus = keyboard._bus
        channel = 13
        _words(bus, TABLE + channel * 16, 0, SOURCE, 0x4008A828, 0)
        bus.WriteDoubleWord(BASE + 8, TABLE)
        bus.WriteDoubleWord(BASE, 1)
        bus.WriteDoubleWord(BASE + 0x20, 1 << channel)
        bus.WriteDoubleWord(BASE + 0x400 + channel * 16, 1)
        bus.WriteDoubleWord(0x4008A800, 1)
        bus.WriteDoubleWord(0x4008A828, 0xA0)
        bus.WriteDoubleWord(0x4008A820, 10)
        bus.WriteDoubleWord(BASE + 0x408 + channel * 16, 0x15)
        assert keyboard.run_for(20).stopped_reason is None
        assert bus.ReadDoubleWord(0x4008A804) == 7
        assert bus.ReadDoubleWord(BASE + 0x58) == 0
        assert bus.ReadDoubleWord(BASE + 0x408 + channel * 16) & 1
        bus.WriteDoubleWord(BASE + 0x78, 1 << channel)
        bus.WriteDoubleWord(0x4008A820, 4)
        assert bus.ReadDoubleWord(0x4008A804) == 1
        assert keyboard.read_memory(TARGET, 4) == struct.pack("<I", 0)


def test_i2c_dma_writes_reach_an_attached_renode_peripheral(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        from Antmicro.Renode.Peripherals.Memory import GenericI2cEeprom
        from System import UInt64

        bus = keyboard._bus
        eeprom = GenericI2cEeprom(UInt64(256), 8, True, 64)
        keyboard._machine.sysbus.flexcomm4.internal.Attach(0x50, eeprom)
        # Address 0x10 and two payload bytes, all sent by DMA to MSTDAT.
        _words(bus, SOURCE, 0x00332210)
        channel = 13
        _words(bus, TABLE + channel * 16, 0, SOURCE + 2, 0x4008A828, 0)
        bus.WriteDoubleWord(BASE + 8, TABLE)
        bus.WriteDoubleWord(BASE, 1)
        bus.WriteDoubleWord(BASE + 0x20, 1 << channel)
        bus.WriteDoubleWord(BASE + 0x400 + channel * 16, 1)
        bus.WriteDoubleWord(0x4008A800, 1)
        bus.WriteDoubleWord(0x4008A828, 0xA0)
        bus.WriteDoubleWord(0x4008A820, 10)
        bus.WriteDoubleWord(BASE + 0x408 + channel * 16, 0x21015)
        assert keyboard.run_for(3).stopped_reason is None
        assert bus.ReadDoubleWord(BASE + 0x58) == 1 << channel
        bus.WriteDoubleWord(0x4008A820, 4)
        assert eeprom.Memory.ReadByte(0x10) == 0x22
        assert eeprom.Memory.ReadByte(0x11) == 0x33
        # Select the address using the CPU, then read through RX DMA channel 12.
        bus.WriteDoubleWord(0x4008A828, 0xA0)
        bus.WriteDoubleWord(0x4008A820, 2)
        bus.WriteDoubleWord(0x4008A828, 0x10)
        bus.WriteDoubleWord(0x4008A820, 1)
        bus.WriteDoubleWord(0x4008A828, 0xA1)
        bus.WriteDoubleWord(0x4008A820, 10)
        channel = 12
        _words(bus, TABLE + channel * 16, 0, 0x4008A828, TARGET + 1, 0)
        bus.WriteDoubleWord(BASE + 0x20, 1 << channel)
        bus.WriteDoubleWord(BASE + 0x400 + channel * 16, 1)
        bus.WriteDoubleWord(BASE + 0x408 + channel * 16, 0x14015)
        assert keyboard.run_for(2).stopped_reason is None
        bus.WriteDoubleWord(0x4008A820, 4)
        assert keyboard.read_memory(TARGET, 2) == b"\x22\x33"
        assert bus.ReadDoubleWord(BASE + 0x58) & (1 << channel)
        assert keyboard.state().stopped_reason is None


def test_dma_bus_error_signals_error_irq_without_completion(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus = keyboard._bus
        dma = keyboard._machine.sysbus.dma0.internal
        # This flash word has no programmed ECC and raises a bus access error.
        _words(bus, TABLE, 0, 0x44000, TARGET, 0)
        bus.WriteDoubleWord(BASE + 8, TABLE)
        bus.WriteDoubleWord(BASE, 1)
        bus.WriteDoubleWord(BASE + 0x20, 1)
        bus.WriteDoubleWord(BASE + 0x408, 0x215)
        assert keyboard.run_for(1).stopped_reason is None
        assert bus.ReadDoubleWord(BASE + 0x40) == 1
        assert bus.ReadDoubleWord(BASE + 0x58) == 0
        assert bus.ReadDoubleWord(BASE + 4) == 4
        assert dma.IRQ.IsSet
        bus.WriteDoubleWord(BASE + 0x40, 1)
        assert not dma.IRQ.IsSet
