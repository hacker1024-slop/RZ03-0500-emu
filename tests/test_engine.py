import os
import struct

import pytest

from razer_emu import Firmware, FlashRegion
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


def test_original_application_needs_initialized_nvram(cpu_image):
    path = os.environ.get("RAZER_TEST_FIRMWARE")
    if not path:
        pytest.skip("Set RAZER_TEST_FIRMWARE to a private raw application image")
    firmware = Firmware.load(path)
    with Keyboard(firmware) as keyboard:
        state = keyboard.run_for(20_000)
        assert state.instructions > 100_000
        assert state.stopped_reason == "cpu_fault"
        assert state.fault_status & 0x8200 == 0x8200
        assert len(firmware.image) < state.fault_address < 0x80000
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


def test_flash_erase_ecc_program_and_reprogram_rejection(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        from Antmicro.Renode.Peripherals.Bus import BusAccessException

        bus = keyboard._bus
        command = 0x40034000
        start, stop, status = command + 0x10, command + 0x14, command + 0xFE0
        clear = command + 0xFE8
        target = 0x44000
        bus.WriteDoubleWord(start, target >> 4)
        bus.WriteDoubleWord(stop, target >> 4)
        bus.WriteDoubleWord(command, 4)
        assert bus.ReadDoubleWord(status) == 4
        bus.WriteDoubleWord(clear, 15)
        bus.WriteDoubleWord(command, 3)
        assert bus.ReadDoubleWord(command + 0x80) == 0
        assert bus.ReadDoubleWord(status) == 12  # DONE + invalid ECC
        with pytest.raises(BusAccessException):
            bus.ReadDoubleWord(target)
        # Load one physical word into the page register; the rest stays zero.
        for i in range(4):
            bus.WriteDoubleWord(command + 0x80 + i * 4, 0x12345678 + i)
        bus.WriteDoubleWord(clear, 15)
        bus.WriteDoubleWord(command, 8)
        bus.WriteDoubleWord(clear, 15)
        bus.WriteDoubleWord(command, 12)
        assert bus.ReadDoubleWord(status) == 4
        assert bus.ReadDoubleWord(target) == 0x12345678
        assert bus.ReadDoubleWord(target + 12) == 0x1234567B
        bus.WriteDoubleWord(clear, 15)
        bus.WriteDoubleWord(command, 5)
        assert bus.ReadDoubleWord(status) == 5  # no longer blank
        bus.WriteDoubleWord(clear, 15)
        bus.WriteDoubleWord(command, 12)
        assert bus.ReadDoubleWord(status) == 5  # cannot program twice without erase
        keyboard.reset()
        assert bus.ReadDoubleWord(target) == 0x12345678  # CPU reset preserves flash


def test_capture_holes_do_not_acquire_valid_flash_ecc(cpu_image):
    region = FlashRegion(0x44000, bytes(range(32)), bytes([1] * 16 + [0] + [1] * 15))
    with Keyboard(Firmware.load(cpu_image), flash_regions=[region]) as keyboard:
        flash = keyboard._machine.sysbus.flash.internal
        assert flash.IsProgrammed(0x44000)
        assert not flash.IsProgrammed(0x44010)
        assert keyboard.read_memory(0x44000, 32) == bytes(range(16)) + bytes(16)


def test_original_application_with_verified_nvram(cpu_image):
    path = os.environ.get("RAZER_TEST_FIRMWARE")
    dumps = os.environ.get("RAZER_TEST_FLASH_DUMPS")
    if not path or not dumps:
        pytest.skip(
            "Set RAZER_TEST_FIRMWARE and RAZER_TEST_FLASH_DUMPS for private-image validation"
        )
    regions = [FlashRegion.load_dump(p) for p in dumps.split(os.pathsep)]
    with Keyboard(Firmware.load(path), flash_regions=regions) as keyboard:
        state = keyboard.run_for(20000)
        assert state.instructions > 100_000
        assert state.exception == 0
        assert state.fault_status == 0
        assert state.stopped_reason == "unsupported_mmio"


def test_watchdog_feed_and_expiration_are_virtual_time_events(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus = keyboard._bus
        base = 0x4000C000
        bus.WriteDoubleWord(base + 4, 0xFF)
        bus.WriteDoubleWord(base, 3)
        for value in (0xAA, 0x55):
            bus.WriteDoubleWord(base + 8, value)
        assert keyboard.run_for(500).stopped_reason is None
        for value in (0xAA, 0x55):
            bus.WriteDoubleWord(base + 8, value)
        assert keyboard.run_for(500).stopped_reason is None
        assert keyboard.run_for(700).stopped_reason == "watchdog_expired"


def test_ezh_mailbox_word_addressing_and_register_operations(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        from System import Array, Byte

        bus = keyboard._bus
        ezh = keyboard._ezh
        code = 0x20028000
        mailbox = 0x20029000
        bus.WriteDoubleWord(mailbox, 0x2002F000)
        bus.WriteDoubleWord(mailbox + 4, 0xDEADBEEF)
        bus.WriteDoubleWord(0x4001D040, mailbox | 2)
        # Original, synthetic architecture-B words; no vendor firmware included.
        words = [
            0x12,  # NOP
            4 | (6 << 10) | (0x1D040 << 12),  # PER_READ R6, ARM2EZH
            0x10 | (6 << 10) | (6 << 20) | (2 << 18) | (2 << 24),  # LSR R6, 2
            0x10 | (6 << 10) | (6 << 20) | (2 << 24),  # LSL R6, 2
            1 | (12 << 10) | (6 << 14) | (1 << 18),  # LDR SP, [R6]
            1 | (7 << 10) | (6 << 14) | (1 << 18) | (1 << 24),  # LDR R7, [R6+4]
            0x18 | (8 << 10) | (8 << 14) | (7 << 24) | (6 << 29),  # set GPO7
            0x18 | (9 << 10) | (9 << 14) | (7 << 24) | (6 << 29),  # output GPD7
            0x18 | (8 << 10) | (8 << 14) | (7 << 24) | (1 << 29),  # clear GPO7
            (1 << 18) | (0x7FF << 20),  # LOAD_IMM R0, -1
        ]
        bus.WriteBytes(Array[Byte](struct.pack(f"<{len(words)}I", *words)), code)
        bus.WriteDoubleWord(0x4001D020, code | 1)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        ezh.StepInstructions(len(words))
        assert ezh.Instructions == len(words)
        assert ezh.GetRegister(12) == 0x2002F000
        assert ezh.GetRegister(7) == 0xDEADBEEF
        assert ezh.GetRegister(6) == mailbox  # loads without WB preserve their pointer
        assert ezh.GetRegister(0) == 0xFFFFFFFF
        assert ezh.GetRegister(8) == 0
        assert ezh.GetRegister(9) == 1 << 7
        assert keyboard.state().stopped_reason is None


def test_ezh_unknown_opcode_stops_at_its_address(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus = keyboard._bus
        bus.WriteDoubleWord(0x20028000, 0x1C)
        bus.WriteDoubleWord(0x4001D020, 0x20028000)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        state = keyboard.run_for(10)
        assert state.stopped_reason == "unsupported_mmio"
        assert state.ezh_pc == 0x20028000
        assert not keyboard._ezh.Running
        assert any("Unsupported EZH instruction" in line for line in keyboard.logs())


def test_ezh_external_flag_conditions(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus = keyboard._bus
        code = 0x20028000
        # NEX and EX are encoded 13 and 15, respectively.
        bus.WriteDoubleWord(code, (1 << 18) | (1 << 20) | (13 << 5))
        bus.WriteDoubleWord(code + 4, (1 << 18) | (2 << 20) | (15 << 5))
        bus.WriteDoubleWord(code + 8, (1 << 18) | (3 << 20) | (15 << 5))
        bus.WriteDoubleWord(0x4001D020, code)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        keyboard._ezh.StepInstructions(2)
        assert keyboard._ezh.GetRegister(0) == 1
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0013)
        keyboard._ezh.StepInstructions(1)
        assert keyboard._ezh.GetRegister(0) == 3
        assert keyboard.state().stopped_reason is None
