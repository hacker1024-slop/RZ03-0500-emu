import os
import struct

import pytest

from razer_emu import Firmware
from razer_emu.core import Keyboard


def test_cortex_m33_stack_limit_fpu_and_reset(cpu_image):
    firmware = Firmware.load(cpu_image)
    with Keyboard(firmware) as keyboard:
        state = keyboard.run_for(1000)
        assert state.stopped_reason is None
        # MRS MSPLIM must return the value written by MSR; FPU 1.0 + 1.0 = 2.0.
        assert struct.unpack("<II", keyboard.read_memory(0x20001000, 8)) == (0x04006000, 0x40000000)
        # MR1 generates periodic interrupts through the NVIC and executes a
        # real Thumb ISR, including exception entry, W1C acknowledgement and return.
        interrupts = struct.unpack("<I", keyboard.read_memory(0x20001008, 4))[0]
        assert 8 <= interrupts <= 10
        keyboard.reset()
        assert keyboard.read_memory(0x20001000, 8) == bytes(8)
        assert keyboard.state().pc == firmware.reset_vector & ~1
        assert keyboard.step(4).stopped_reason is None
    with pytest.raises(RuntimeError, match="closed"):
        keyboard.state()


def test_unknown_mmio_is_reported_and_stops_run(cpu_image):
    # Thumb: ldr r0,[pc,#4]; ldr r1,[r0]; b .; nop; .word 0x400ff000
    image = bytearray(Firmware.load(cpu_image).image)
    struct.pack_into("<I", image, 4, 0x20000141)
    image[0x140:0x14C] = bytes.fromhex("01480168fee700bf00f00f40")
    with Keyboard(Firmware.from_bytes(image)) as keyboard:
        state = keyboard.run_for(100)
        assert state.stopped_reason == "unsupported_mmio"
        assert state.unknown_reads > 0
        assert state.elapsed_us == 10
        assert any("400FF000" in line.upper() for line in keyboard.logs())
        with pytest.raises(RuntimeError, match="reset"):
            keyboard.run_for(100)


def test_original_application_executes_until_missing_board_model(cpu_image):
    path = os.environ.get("RAZER_TEST_FIRMWARE")
    if not path:
        pytest.skip("Set RAZER_TEST_FIRMWARE to a private raw application image")
    firmware = Firmware.load(path)
    with Keyboard(firmware) as keyboard:
        state = keyboard.run_for(20_000)
        assert state.instructions > 100_000
        assert state.exception == 0
        assert state.fault_status == 0
        assert state.stopped_reason == "unsupported_mmio"
        assert state.elapsed_us < 20_000


def test_gpio_aliases_flash_reads_and_i2c_nack(cpu_image):
    firmware = Firmware.load(cpu_image)
    with Keyboard(firmware) as keyboard:
        bus = keyboard._bus
        # LPC55 byte aliases and word PIN must observe the same output latch.
        bus.WriteDoubleWord(0x4008E000, 1 << 5)
        bus.WriteByte(0x4008C005, 1)
        assert bus.ReadByte(0x4008C005) == 1
        assert bus.ReadDoubleWord(0x4008D014) == 1
        bus.WriteByte(0x4008C005, 0)
        assert bus.ReadByte(0x4008C005) == 0
        # Flash command addresses are 16-byte words, not byte addresses.
        bus.WriteDoubleWord(0x40034010, 0x800)
        bus.WriteDoubleWord(0x40034000, 3)
        assert bus.ReadDoubleWord(0x40034080) == firmware.initial_sp
        assert bus.ReadDoubleWord(0x40034FE0) & 4
        # An absent I2C target must NACK its address, rather than faking ACK.
        bus.WriteDoubleWord(0x4008A800, 1)
        bus.WriteDoubleWord(0x4008A828, 0xA0)
        bus.WriteDoubleWord(0x4008A820, 2)
        assert bus.ReadDoubleWord(0x4008A804) == 7
        bus.WriteDoubleWord(0x4008A820, 4)
        assert bus.ReadDoubleWord(0x4008A804) == 1
        assert keyboard.state().stopped_reason is None
