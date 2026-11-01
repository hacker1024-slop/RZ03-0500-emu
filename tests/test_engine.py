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


@pytest.mark.parametrize("duration", [11, 17, 1007])
def test_bounded_runs_end_at_partial_sync_quantum(cpu_image, duration):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        for _ in range(3):
            before = keyboard.state().elapsed_us
            state = keyboard.run_for(duration)
            assert state.stopped_reason is None
            assert state.elapsed_us == before + duration


@pytest.mark.parametrize("interval_us", [10, 125])
def test_unknown_mmio_is_reported_and_stops_run(cpu_image, interval_us):
    # Thumb: ldr r0,[pc,#4]; ldr r1,[r0]; b .; nop; .word 0x400ff000
    image = bytearray(Firmware.load(cpu_image).image)
    struct.pack_into("<I", image, 4, 0x20000141)
    image[0x140:0x14C] = bytes.fromhex("01480168fee700bf00f00f40")
    with Keyboard(Firmware.from_bytes(image), check_interval_us=interval_us) as keyboard:
        state = keyboard.run_for(500)
        assert state.stopped_reason == "unsupported_mmio"
        assert state.unknown_reads > 0
        assert state.elapsed_us == interval_us
        assert any("400FF000" in line.upper() for line in keyboard.logs())
        with pytest.raises(RuntimeError, match="reset"):
            keyboard.run_for(100)


def test_pmc_always_on_storage_survives_mcu_reset_but_not_new_board(cpu_image):
    firmware = Firmware.load(cpu_image)
    with Keyboard(firmware) as keyboard:
        bus, address = keyboard._bus, 0x40020084
        assert bus.ReadDoubleWord(address) == 0
        bus.WriteDoubleWord(address, 0xA55A1234)
        assert bus.ReadDoubleWord(0x50020084) == 0xA55A1234
        keyboard.reset()
        assert bus.ReadDoubleWord(address) == 0xA55A1234
        assert keyboard.state().stopped_reason is None
    with Keyboard(firmware) as keyboard:
        assert keyboard._bus.ReadDoubleWord(address) == 0


@pytest.mark.parametrize("pin", [20, 32, 60])
def test_external_gpio_input_overrides_pulls_and_survives_mcu_reset(cpu_image, pin):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        gpio, bus = keyboard._machine.sysbus.gpio.internal, keyboard._bus
        iocon, alias = 0x40001000 + pin * 4, 0x4008C000 + pin
        bus.WriteDoubleWord(iocon, 0x120)
        assert bus.ReadByte(alias) == 1
        gpio.OnGPIO(pin, False)
        bus.WriteDoubleWord(iocon, 0x120)
        assert bus.ReadByte(alias) == 0  # weak pull-up cannot overwrite external low
        gpio.OnGPIO(pin, True)
        bus.WriteDoubleWord(iocon, 0x110)
        assert bus.ReadByte(alias) == 1  # weak pull-down cannot overwrite external high
        keyboard.reset()
        assert bus.ReadByte(alias) == 1
        bus.WriteDoubleWord(iocon, 0x110)
        gpio.ReleaseInput(pin)
        assert bus.ReadByte(alias) == 0
        bus.WriteDoubleWord(iocon, 0x120)
        assert bus.ReadByte(alias) == 1
        assert keyboard.state().stopped_reason is None


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


@pytest.mark.parametrize("vbus", [False, True])
def test_original_application_with_verified_nvram(cpu_image, vbus):
    path = os.environ.get("RAZER_TEST_FIRMWARE")
    dumps = os.environ.get("RAZER_TEST_FLASH_DUMPS")
    if not path or not dumps:
        pytest.skip(
            "Set RAZER_TEST_FIRMWARE and RAZER_TEST_FLASH_DUMPS for private-image validation"
        )
    regions = [FlashRegion.load_dump(p) for p in dumps.split(os.pathsep)]
    with Keyboard(Firmware.load(path), flash_regions=regions) as keyboard:
        device = keyboard._machine.sysbus.usbDevice.internal
        device.VbusPresent = vbus
        state = keyboard.run_for(50000)
        assert state.instructions > 100_000
        assert state.elapsed_us >= 20000
        assert state.exception == 0
        assert state.fault_status == 0
        assert state.stopped_reason is None
        assert device.Enabled
        # The original IRQ handler handles host attachment and requests connect;
        # the controller model never sets the software DCON control itself.
        assert bool(device.ReadDoubleWord(0) & 0x10000) == vbus


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


def test_original_ezh_irq_handler_acknowledges_shared_ram_handshake(cpu_image):
    path = os.environ.get("RAZER_TEST_FIRMWARE")
    dumps = os.environ.get("RAZER_TEST_FLASH_DUMPS")
    if not path or not dumps:
        pytest.skip("Set private firmware and flash captures for original-handler validation")
    regions = [FlashRegion.load_dump(p) for p in dumps.split(os.pathsep)]
    with Keyboard(Firmware.load(path), flash_regions=regions) as keyboard:
        assert keyboard.run_for(50_000).stopped_reason is None
        bus = keyboard._bus
        assert bus.ReadDoubleWord(0xE000E100) & (1 << 30)
        # Hold the scanner stopped, set EX, and request the ordinary mailbox
        # completion IRQ. The original Thumb ISR must clear EX and publish its
        # matrix-ready flag; neither response is supplied by the peripheral.
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0012)
        bus.WriteByte(0x20024E52, 0)
        bus.WriteDoubleWord(0x4001D040, (bus.ReadDoubleWord(0x4001D040) & ~3) | 2)
        bus.WriteDoubleWord(0x4001D044, 0)
        assert bus.ReadDoubleWord(0xE000E200) & (1 << 30)
        for _ in range(500):
            assert keyboard.step().stopped_reason is None
            if bus.ReadByte(0x20024E52):
                break
        assert bus.ReadByte(0x20024E52) == 1
        assert bus.ReadDoubleWord(0x4001D024) & 2 == 0


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
        ezh.AdvanceCycles(len(words))
        assert ezh.Instructions == len(words)
        assert ezh.GetRegister(12) == 0x2002F000
        assert ezh.GetRegister(7) == 0xDEADBEEF
        assert ezh.GetRegister(6) == mailbox  # loads without WB preserve their pointer
        assert ezh.GetRegister(0) == 0xFFFFFFFF
        assert ezh.GetRegister(8) == 0
        assert ezh.GetRegister(9) == 1 << 7
        assert keyboard.state().stopped_reason is None


def test_ezh_breakpoint_replaces_instruction_without_call(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus, ezh = keyboard._bus, keyboard._ezh
        code, handler = 0x20028000, 0x20028100

        def load(value):
            return (1 << 18) | (value << 20)

        bus.WriteDoubleWord(code, load(1))
        bus.WriteDoubleWord(code + 4, load(2))
        bus.WriteDoubleWord(handler, load(3))
        bus.WriteDoubleWord(0x4001D020, code | 1)
        bus.WriteDoubleWord(0x4001D030, code | 3)
        bus.WriteDoubleWord(0x4001D034, handler | 1)
        assert bus.ReadDoubleWord(0x4001D020) == code
        assert bus.ReadDoubleWord(0x4001D030) == code
        assert bus.ReadDoubleWord(0x4001D034) == handler
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        ezh.AdvanceCycles(1)
        assert ezh.GetRegister(0) == 3
        assert ezh.GetRegister(15) == 0
        assert ezh.GetRegister(13) == handler + 4
        # Removing the breakpoint permits the original instruction to execute.
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0010)
        bus.WriteDoubleWord(0x4001D030, 0)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        ezh.AdvanceCycles(2)
        assert ezh.GetRegister(0) == 2
        assert keyboard.state().stopped_reason is None


def test_ezh_unknown_opcode_stops_at_its_address(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus = keyboard._bus
        bus.WriteDoubleWord(0x20028000, 0x1E)
        bus.WriteDoubleWord(0x4001D020, 0x20028000)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        state = keyboard.run_for(10)
        assert state.stopped_reason == "unsupported_ezh_instruction"
        assert state.ezh_pc == 0x20028000
        assert state.unknown_reads == state.unknown_writes == 0
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
        keyboard._ezh.AdvanceCycles(2)
        assert keyboard._ezh.GetRegister(0) == 1
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0013)
        keyboard._ezh.AdvanceCycles(1)
        assert keyboard._ezh.GetRegister(0) == 3
        assert keyboard.state().stopped_reason is None


def test_ezh_heartbeat_wait_and_synchronized_execution(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        from System import Array, Byte

        bus = keyboard._bus
        code = 0x20028000
        words = [
            0x32 | (4 << 16),  # heartbeat every four EZH cycles
            0x72,  # wait for next beat
            (1 << 18) | (1 << 20),  # R0 = 1
            0x80000052,  # synchronize every instruction to heartbeat
            (1 << 18) | (2 << 20),  # R0 = 2
            0x52,  # disable synchronized execution
            (1 << 18) | (3 << 20),  # R0 = 3
        ]
        bus.WriteBytes(Array[Byte](struct.pack(f"<{len(words)}I", *words)), code)
        bus.WriteDoubleWord(0x4001D020, code)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        ezh = keyboard._ezh
        ezh.AdvanceCycles(4)
        assert ezh.Instructions == 2 and ezh.GetRegister(0) == 0
        ezh.AdvanceCycles(2)
        assert ezh.GetRegister(0) == 1
        ezh.AdvanceCycles(3)
        assert ezh.GetRegister(0) == 2
        ezh.AdvanceCycles(4)
        assert ezh.GetRegister(0) == 2  # sync disable waits for its own beat
        ezh.AdvanceCycles(1)
        assert ezh.GetRegister(0) == 3


def test_ezh_scheduled_pc_write_executes_two_delay_slots(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        from System import Array, Byte

        bus = keyboard._bus
        code = 0x20028000
        words = [
            6 | (13 << 10) | (13 << 14) | (1 << 18) | (8 << 20) | 512,
            (1 << 18) | (1 << 20),  # delay slot 1: R0 = 1
            (1 << 18) | (1 << 10) | (2 << 20),  # delay slot 2: R1 = 2
            (1 << 18) | (3 << 20),  # skipped: R0 = 3
            (1 << 18) | (2 << 10) | (4 << 20),  # destination: R2 = 4
        ]
        bus.WriteBytes(Array[Byte](struct.pack(f"<{len(words)}I", *words)), code)
        bus.WriteDoubleWord(0x4001D020, code)
        # A flushed instruction must not enter the breakpoint handler.
        bus.WriteDoubleWord(0x4001D030, code + 12)
        bus.WriteDoubleWord(0x4001D034, code + 12)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        ezh = keyboard._ezh
        ezh.AdvanceCycles(4)
        assert [ezh.GetRegister(i) for i in range(3)] == [1, 2, 4]
        assert ezh.ReadDoubleWord(0x28) == code + 20
        assert keyboard.state().stopped_reason is None


def test_ezh_absolute_call_return_and_conditional_decode(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        from System import Array, Byte

        bus = keyboard._bus
        code = 0x20028000
        words = [
            (code + 24) | 3,  # GOSUB: address overlaps opcode and condition fields
            (1 << 18) | (2 << 20),  # returned: R0 = 2
            0x1E | (15 << 5),  # unsupported opcode, EX false: no side effects
            (1 << 18) | (1 << 10) | (3 << 20),  # R1 = 3
            0x15 | (13 << 14),  # branch past the subroutine
            0x12,
            (1 << 18) | (1 << 20),  # subroutine: R0 = 1
            0x15 | (15 << 14),  # return through RA
        ]
        bus.WriteBytes(Array[Byte](struct.pack(f"<{len(words)}I", *words)), code)
        bus.WriteDoubleWord(0x4001D020, code)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        ezh = keyboard._ezh
        ezh.AdvanceCycles(7)
        assert ezh.GetRegister(15) == code + 4
        assert [ezh.GetRegister(i) for i in range(2)] == [2, 3]
        assert ezh.GetRegister(13) == code + 24
        assert keyboard.state().stopped_reason is None


def test_ezh_interrupt_channels_and_mailbox_handshake(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus = keyboard._bus
        ezh = keyboard._ezh
        nvic = keyboard._machine.sysbus.nvic.internal
        # The common output uses the board's IRQ30 connection. Additional
        # channel outputs get explicit synthetic consumers for this test.
        for channel, interrupt in ((0, 2), (1, 3), (2, 4), (23, 5)):
            ezh.Connections[channel].Connect(nvic, interrupt)
        code = 0x20028000
        bus.WriteDoubleWord(code, 0x14 | (0x800003 << 8))
        bus.WriteDoubleWord(code + 4, 0x14)  # common interrupt even with mask 0
        bus.WriteDoubleWord(0x4001D020, code)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        ezh.AdvanceCycles(1)
        mask, common = (1 << 30) | 0x3C, 1 << 30
        assert bus.ReadDoubleWord(0xE000E200) & mask == common | 0x2C
        bus.WriteDoubleWord(0xE000E280, mask)
        ezh.AdvanceCycles(1)
        assert bus.ReadDoubleWord(0xE000E200) & mask == common
        bus.WriteDoubleWord(0xE000E280, mask)
        for mode in (0, 1, 3):
            bus.WriteDoubleWord(0x4001D040, 0x20029000 | mode)
            bus.WriteDoubleWord(0x4001D044, 0xA5A50000 | mode)
            assert bus.ReadDoubleWord(0xE000E200) & common == 0
        bus.WriteDoubleWord(0x4001D040, 0x20029002)
        bus.WriteDoubleWord(0x4001D044, 0xDEADBEEF)
        assert bus.ReadDoubleWord(0x4001D044) == 0xDEADBEEF
        assert bus.ReadDoubleWord(0xE000E200) & common == common
        assert keyboard.state().stopped_reason is None


@pytest.mark.parametrize("fetch", [False, True])
def test_ezh_flash_bus_fault_is_separate_from_cortex_m_fault(cpu_image, fetch):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus = keyboard._bus
        code = 0x20028000
        missing = 0x30000  # an unprogrammed flash word, with invalid ECC
        bus.WriteDoubleWord(code, (1 << 18) | (3 << 20) | (1 << 19))  # R0 = 0x30000
        bus.WriteDoubleWord(code + 4, 1 | (1 << 10) | (1 << 18))  # LDR R1, [R0]
        bus.WriteDoubleWord(0x4001D020, missing if fetch else code)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        state = keyboard.run_for(10)
        assert state.stopped_reason == "ezh_bus_fault"
        assert state.ezh_pc == (missing if fetch else code + 4)
        assert state.ezh_fault_address == missing
        assert state.ezh_instructions == (0 if fetch else 1)
        assert state.fault_status == 0 and state.fault_address is None
        assert not keyboard._ezh.Running
        keyboard.reset()
        assert keyboard.state().ezh_fault_address is None
        assert keyboard.state().stopped_reason is None


@pytest.mark.parametrize("repeats", [0, 2])
def test_ezh_tight_loop_prologue_runs_once_and_end_is_exclusive(cpu_image, repeats):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        from System import Array, Byte

        bus = keyboard._bus
        code = 0x20028000
        words = [
            6 | (4 << 10) | (13 << 14) | (1 << 18) | (12 << 20),  # R4 = end
            (1 << 18) | (3 << 10) | (repeats << 20),  # R3 = extra iterations
            0x1A | (4 << 14) | (3 << 20),  # TIGHT_LOOP R4, R3
            6 | (1 << 10) | (1 << 14) | (1 << 18) | (1 << 20),  # prologue: R1++
            6 | (1 << 18) | (1 << 20),  # body (one instruction): R0++
            (1 << 18) | (2 << 10) | (7 << 20),  # exclusive end: R2 = 7
        ]
        bus.WriteBytes(Array[Byte](struct.pack(f"<{len(words)}I", *words)), code)
        bus.WriteDoubleWord(0x4001D020, code)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        ezh = keyboard._ezh
        ezh.AdvanceCycles(len(words) + repeats)
        assert [ezh.GetRegister(i) for i in range(3)] == [repeats + 1, 1, 7]
        assert ezh.GetRegister(3) == repeats  # supervisor keeps a private counter
        assert ezh.GetRegister(13) == code + len(words) * 4
        assert keyboard.state().stopped_reason is None


def test_ezh_boolean_modes_sticky_clear_and_mux_status(cpu_image):
    with Keyboard(Firmware.load(cpu_image)):
        from Antmicro.Renode.Peripherals.Miscellaneous import RazerEzhBoolean

        detector = RazerEzhBoolean()
        levels = [False, True, True, False, False, True, False]
        expected = [
            [1, 1, 1, 1, 1, 1, 1],  # always high
            [0, 1, 1, 1, 1, 1, 1],  # sticky rising
            [0, 0, 0, 1, 1, 1, 1],  # sticky falling
            [0, 1, 1, 1, 1, 1, 1],  # sticky either edge
            [0, 1, 1, 0, 0, 1, 0],  # high level
            [1, 0, 0, 1, 1, 0, 1],  # low level
            [0, 0, 0, 0, 0, 0, 0],  # always low
            [0, 1, 0, 1, 0, 1, 1],  # nonsticky either edge
        ]
        for mode, results in enumerate(expected):
            detector.Reset()
            detector.Selection = (3 << 8) | 0xFF  # slice 0 observes input 3
            assert detector.Selection == 3 << 8  # low status bits are read-only
            # Suppress the otherwise constant-true trailing AND term.
            detector.Mode = 1 | (mode << 8) | (6 << 29)
            actual = []
            for level in levels:
                detector.SetInput(3, level)
                detector.Sample(0)
                actual.append(detector.Results)
                assert detector.Selection & 1 == int(level)
            assert actual == results
            if mode in (1, 2, 3):
                detector.Mode = detector.Mode  # any CFM write clears sticky state
                detector.Sample(0)
                assert detector.Results == 0


def test_ezh_boolean_cascade_and_pending_trap_override(cpu_image):
    with Keyboard(Firmware.load(cpu_image)):
        from Antmicro.Renode.Peripherals.Miscellaneous import RazerEzhBoolean

        detector = RazerEzhBoolean()
        detector.Selection = sum(i << (8 + 3 * i) for i in range(8))
        detector.Mode = sum(4 << (8 + 3 * i) for i in range(8)) | 0x8C
        # NXP's ABC + D + EFGH example: only each group's final slice routes out.
        for pin in (0, 1):
            detector.SetInput(pin, True)
        detector.Sample(0)
        assert detector.Results == 0
        detector.SetInput(2, True)
        detector.Sample(0)
        assert detector.Results == 4
        for pin in range(3, 8):
            detector.SetInput(pin, True)
        detector.Sample(0)
        assert detector.Results == 0x8C
        detector.SetInput(0, False)
        detector.Sample(0)
        assert detector.Results == 0x88
        detector.Mode = sum(6 << (8 + 3 * i) for i in range(8)) | 0xFF
        detector.Sample((1 << 19) | (1 << 3))
        assert detector.Results == 8
        detector.Sample((1 << 19) | (1 << 11) | (1 << 3))
        assert detector.Results == 8  # polarity does not invert software status
        detector.Sample(1 << 19)
        assert detector.Results == 0


def test_ezh_pending_trap_edge_polarity_latches_until_software_clear(cpu_image):
    with Keyboard(Firmware.load(cpu_image)):
        from Antmicro.Renode.Peripherals.Miscellaneous import RazerEzhBoolean

        detector = RazerEzhBoolean()
        trap = 1 << 9  # input 1 falling edge; other inputs rising edge
        detector.SetInput(0, True)
        detector.SetInput(1, True)
        trap = detector.Sample(trap)
        assert trap & 3 == 1
        detector.SetInput(0, False)
        detector.SetInput(1, False)
        trap = detector.Sample(trap)
        assert trap & 3 == 3
        assert detector.Sample(trap) & 3 == 3  # remains latched without edges
        trap &= ~3
        assert detector.Sample(trap) & 3 == 0


def test_ezh_boolean_final_slice_terminates_an_unmasked_term(cpu_image):
    with Keyboard(Firmware.load(cpu_image)):
        from Antmicro.Renode.Peripherals.Miscellaneous import RazerEzhBoolean

        detector = RazerEzhBoolean()
        # Hardware with CFS=CFM=0 asserts BS and computes the vector-7 address.
        detector.Sample(0)
        assert detector.Results == 0x80
        detector.Mode = 1
        detector.Sample(0)
        assert detector.Results == 0x81
        detector.Mode = 6 << 29  # final slice constant low suppresses the term
        detector.Sample(0)
        assert detector.Results == 0


@pytest.mark.parametrize("word,destination", [(0x0000001C, 0), (0x2000161C, 5)])
def test_ezh_hold_can_compute_vector_address_without_branch(cpu_image, word, destination):
    # These two encodings were compared on LPC5528 hardware with CFM=1.
    # Both continue sequentially and write table+4 into their encoded destination.
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus, ezh = keyboard._bus, keyboard._ezh
        code, table = 0x20028000, 0x20028100
        bus.WriteDoubleWord(code, 1 | (13 << 14) | (1 << 18) | (6 << 24))
        bus.WriteDoubleWord(code + 4, (1 << 18) | (11 << 10) | (1 << 20))
        bus.WriteDoubleWord(code + 8, word)
        bus.WriteDoubleWord(code + 12, (1 << 18) | (1 << 10) | (7 << 20))
        bus.WriteDoubleWord(code + 32, table)
        bus.WriteDoubleWord(0x4001D020, code)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        ezh.AdvanceCycles(4)
        assert ezh.GetRegister(destination) == table + 4
        assert ezh.GetRegister(1) == 7
        assert ezh.GetRegister(15) == code + 12
        assert ezh.GetRegister(13) == code + 16
        assert not ezh.Holding
        assert keyboard.state().stopped_reason is None


def test_ezh_hold_waits_for_trigger_and_resumes(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus = keyboard._bus
        code = 0x20028000
        bus.WriteDoubleWord(code, 1 | (11 << 10) | (13 << 14) | (1 << 18) | (6 << 24))
        bus.WriteDoubleWord(code + 32, 0xC0000101)  # rising slice 0, trailing term off
        bus.WriteDoubleWord(code + 4, 0x1C | (13 << 10) | (1 << 15))  # HOLD
        bus.WriteDoubleWord(code + 8, (1 << 18) | (7 << 20))
        bus.WriteDoubleWord(0x4001D020, code)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        ezh = keyboard._ezh
        ezh.AdvanceCycles(5)
        assert ezh.Instructions == 1 and ezh.GetRegister(13) == code + 4
        assert keyboard.state().ezh_holding
        assert keyboard.state().stopped_reason is None
        ezh.OnGPIO(0, True)
        ezh.AdvanceCycles(1)
        assert ezh.Instructions == 2 and ezh.GetRegister(13) == code + 8
        assert not keyboard.state().ezh_holding
        assert ezh.GetRegister(10) & 0xFF == 0xFF  # all slices select input 0
        ezh.AdvanceCycles(1)
        assert ezh.GetRegister(0) == 7


@pytest.mark.parametrize(
    "vector,large,accelerated,no_return",
    [
        (1, False, False, False),
        (2, True, False, True),
        (1, False, True, True),
        (2, True, True, False),
    ],
)
def test_ezh_vectored_hold_dispatch_and_unflushed_slots(
    cpu_image, vector, large, accelerated, no_return
):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        from System import Array, Byte

        code = 0x20028000
        hold = 0x1C | (13 << 10) | (13 << 20)
        hold |= (int(large) << 19) | (int(no_return) << 18)
        if accelerated:
            hold |= 512 | (1 << (24 + vector))
        target_index = 4 + vector * (4 if large else 1)
        words = [0x12] * (target_index + 1)
        # Both this vector and vector 7 fire; the lower-numbered vector wins.
        words[0] = (1 << 18) | (11 << 10) | (((1 << vector) | 128) << 20)  # CFM
        words[1] = hold
        words[2] = (1 << 18) | (1 << 20)
        words[3] = (1 << 18) | (1 << 10) | (2 << 20)
        words[target_index] = (1 << 18) | (2 << 10) | (9 << 20)
        bus = keyboard._bus
        bus.WriteBytes(Array[Byte](struct.pack(f"<{len(words)}I", *words)), code)
        bus.WriteDoubleWord(0x4001D020, code)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        ezh = keyboard._ezh
        ezh.AdvanceCycles(4 if accelerated else 2)
        assert ezh.GetRegister(13) == code + target_index * 4
        assert [ezh.GetRegister(i) for i in range(2)] == ([1, 2] if accelerated else [0, 0])
        expected_return = 0 if no_return else code + (16 if accelerated else 8)
        assert ezh.GetRegister(15) == expected_return
        ezh.AdvanceCycles(1)
        assert ezh.GetRegister(2) == 9
        assert keyboard.state().stopped_reason is None


def test_ezh_accelerated_hold_preserves_branch_issued_in_delay_slot(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        from System import Array, Byte

        code = 0x20028000
        toggle = 0x18 | (8 << 10) | (8 << 14) | (7 << 29)
        words = [
            (1 << 18) | (11 << 10) | (4 << 20),  # CFM: vector 2 is active
            0x1C | (13 << 10) | (13 << 20) | (1 << 18) | 512 | (4 << 24),
            8 | (13 << 10) | (13 << 14) | (1 << 18) | (12 << 20) | 512,
            toggle,  # second delay slot
            (1 << 18) | (99 << 20),  # vector 0: must not execute
            0x12,  # vector 1
            toggle,  # vector 2: executes before the second scheduled branch
        ]
        bus = keyboard._bus
        bus.WriteBytes(Array[Byte](struct.pack(f"<{len(words)}I", *words)), code)
        bus.WriteDoubleWord(0x4001D020, code)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        ezh = keyboard._ezh
        ezh.AdvanceCycles(5)
        assert ezh.GetRegister(13) == code + 4
        assert ezh.GetRegister(8) == 0  # both toggle instructions executed
        assert ezh.GetRegister(0) == 0
        assert keyboard.state().stopped_reason is None


def test_ezh_reignition_discards_old_hold_pipeline(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus = keyboard._bus
        code = 0x20028000
        # Configure a constant-false term before testing a waiting HOLD.
        bus.WriteDoubleWord(code - 4, (1 << 18) | (11 << 10) | (8 << 14) | (6 << 20))
        bus.WriteDoubleWord(code, 0x1C | (13 << 10) | (1 << 15))
        bus.WriteDoubleWord(code + 4, (1 << 18) | (9 << 20))
        bus.WriteDoubleWord(0x4001D020, code - 4)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        ezh = keyboard._ezh
        ezh.AdvanceCycles(2)
        assert ezh.Holding and ezh.Instructions == 1
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0010)
        bus.WriteDoubleWord(0x4001D020, code + 4)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        ezh.AdvanceCycles(1)
        assert not ezh.Holding and ezh.Instructions == 2
        assert ezh.GetRegister(0) == 9 and ezh.GetRegister(13) == code + 8


def test_usb_phy_power_pll_aliases_and_usb_ram_reset(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus = keyboard._bus
        phy = keyboard._machine.sysbus.usbPhy.internal
        base = 0x40038000
        assert not phy.Ready and not phy.PllLocked
        assert bus.ReadDoubleWord(base) == 0x1E1C00
        # A software write cannot manufacture PLL_LOCK or a host connection.
        bus.WriteDoubleWord(base + 0xA4, 0x80000000)
        bus.WriteDoubleWord(base + 0x40, 0xFFFFFFFF)
        assert bus.ReadDoubleWord(base + 0xA0) & 0x80000000 == 0
        assert bus.ReadDoubleWord(base + 0x40) == 0
        # Power + regulator + the reset-enabled PLL provide functional lock.
        bus.WriteDoubleWord(base + 0xA4, 0x201000)
        assert phy.PllLocked
        assert bus.ReadDoubleWord(base + 0xA0) & 0x80000000
        bus.WriteDoubleWord(base + 0xA8, 0x2000)  # disable PLL
        assert not phy.PllLocked
        bus.WriteDoubleWord(base + 0xA4, 0x2000)
        bus.WriteDoubleWord(base + 0xA4, 0x80000)  # select explicit bias power-down
        assert not phy.PllLocked
        bus.WriteDoubleWord(base + 0xA8, 0x100000)
        assert phy.PllLocked
        phy.ReferenceClockPresent = False
        assert not phy.PllLocked
        phy.ReferenceClockPresent = True
        bus.WriteDoubleWord(base + 0xA4, 0x40)
        bus.WriteDoubleWord(base + 0x38, 0xC0000000)  # clear reset and clock gate
        bus.WriteDoubleWord(base, 0)  # power up transmit/receive blocks
        assert phy.Ready
        bus.WriteDoubleWord(base + 0x34, 0x80000000)
        assert not phy.Ready and bus.ReadDoubleWord(base + 0x30) & 0x40000000
        bus.WriteDoubleWord(base + 0x38, 0x80000000)
        assert not phy.Ready  # clock gate remains set after reset is released
        bus.WriteDoubleWord(base + 0x38, 0x40000000)
        assert phy.Ready
        bus.WriteDoubleWord(base + 0x14, 0xF)
        bus.WriteDoubleWord(base + 0x18, 2)
        bus.WriteDoubleWord(base + 0x1C, 4)
        assert bus.ReadDoubleWord(base + 0x10) & 0xF == 9
        bus.WriteDoubleWord(0x40100000, 0x12345678)
        bus.WriteDoubleWord(0x40103FFC, 0xDEADBEEF)
        assert keyboard.read_memory(0x40100000, 4) == struct.pack("<I", 0x12345678)
        assert keyboard.read_memory(0x40103FFC, 4) == struct.pack("<I", 0xDEADBEEF)
        assert keyboard.state().stopped_reason is None
        keyboard.reset()
        assert not phy.Ready and not phy.PllLocked
        assert keyboard.read_memory(0x40103FFC, 4) == bytes(4)


def test_usb_device_interrupt_acknowledgement_and_endpoint_abort(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus = keyboard._bus
        device = keyboard._machine.sysbus.usbDevice.internal
        base = 0x40094000
        bus.WriteDoubleWord(base + 0x28, 4)  # software-set EP1 OUT interrupt
        assert bus.ReadDoubleWord(base + 0x20) == 4 and not device.IRQ.IsSet
        bus.WriteDoubleWord(base + 0x24, 4)
        assert device.IRQ.IsSet
        assert bus.ReadDoubleWord(0xE000E204) & (1 << 15)  # NVIC IRQ47
        bus.WriteDoubleWord(base + 0x20, 4)  # W1C
        assert not device.IRQ.IsSet and bus.ReadDoubleWord(base + 0x20) == 0
        bus.WriteDoubleWord(base, 0x10C000AA)  # attempt to write physical status
        assert device.Address == 42 and not device.Enabled  # PHY is powered down
        assert bus.ReadDoubleWord(base) & 0x10C00000 == 0
        device.VbusPresent = True
        bus.WriteDoubleWord(base, 0xAA)
        assert bus.ReadDoubleWord(base) & 0x10000000  # RO input survives CPU RMW
        assert not device.IRQ.IsSet
        # Aborting a channel cancels both of its buffers, preserving other fields
        # and leaving neighboring channels untouched.
        table = 0x40100100
        bus.WriteDoubleWord(base + 8, table)
        command = 0x80000000 | (17 << 11) | 8
        for offset in (16, 20, 24):
            bus.WriteDoubleWord(table + offset, command)
        bus.WriteDoubleWord(base + 0x14, 4)
        assert bus.ReadDoubleWord(base + 0x14) == 0
        assert bus.ReadDoubleWord(table + 16) == command & ~0x80000000
        assert bus.ReadDoubleWord(table + 20) == command & ~0x80000000
        assert bus.ReadDoubleWord(table + 24) == command
        assert keyboard.state().stopped_reason is None
        # Invalid descriptor placement must not DMA into arbitrary MMIO.
        bus.WriteDoubleWord(base + 8, 0x40000000)
        bus.WriteDoubleWord(base + 0x14, 4)
        assert keyboard.state().stopped_reason == "unsupported_mmio"
        assert any("endpoint list outside USB RAM" in line for line in keyboard.logs())
        keyboard.reset()
        assert device.VbusPresent and bus.ReadDoubleWord(base) & 0x10000000


def test_usb_vbus_change_interrupt_and_read_only_attachment_status(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus = keyboard._bus
        device = keyboard._machine.sysbus.usbDevice.internal
        base = 0x40094000
        bus.WriteDoubleWord(0x40038038, 0xC0000000)
        bus.WriteDoubleWord(0x40038000, 0)
        bus.WriteDoubleWord(0x400380A4, 0x203040)
        bus.WriteDoubleWord(base + 0x24, 0x80000000)
        device.VbusPresent = True
        assert not device.IRQ.IsSet  # controller has not been enabled yet
        bus.WriteDoubleWord(base, 0x80)
        assert device.IRQ.IsSet
        assert bus.ReadDoubleWord(base) & 0x18000000 == 0x18000000
        assert bus.ReadDoubleWord(base + 0x10) & 0x100000
        assert bus.ReadDoubleWord(base + 0x20) == 0x80000000
        assert bus.ReadDoubleWord(base) & 0x04000000 == 0  # no synthetic bus reset
        bus.WriteDoubleWord(base, 0x08000080)  # W1C OTG_C, preserve enable
        bus.WriteDoubleWord(base + 0x20, 0x80000000)
        assert not device.IRQ.IsSet
        bus.WriteDoubleWord(base + 0x10, 0)
        assert bus.ReadDoubleWord(base + 0x10) & 0x100000  # ADPPROBE is physical
        device.VbusPresent = False
        assert device.IRQ.IsSet
        assert bus.ReadDoubleWord(base) & 0x10000000 == 0
        assert bus.ReadDoubleWord(base + 0x10) & 0x100000 == 0
        assert keyboard.state().stopped_reason is None


@pytest.mark.parametrize(
    "operation,expected", [(0, 12), (1, 4), (2, 29), (3, 25), (4, 33), (5, 0xFFFFFFF7)]
)
def test_ezh_fused_shift_uses_shifted_value_as_left_operand(cpu_image, operation, expected):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus = keyboard._bus
        code = 0x20028000
        words = [
            (1 << 18) | (1 << 10) | (21 << 20),  # R1 = 21
            (1 << 18) | (2 << 10) | (3 << 20),  # R2 = 3
            0x10 | (1 << 14) | (2 << 20) | (2 << 24) | (operation << 29) | 512,
            # Copy on AZ to observe flags from the fused result.
            (1 << 18) | (3 << 10) | (7 << 20) | (1 << 5),
        ]
        for index, word in enumerate(words):
            bus.WriteDoubleWord(code + index * 4, word)
        bus.WriteDoubleWord(0x4001D020, code)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        keyboard._ezh.AdvanceCycles(len(words))
        assert keyboard._ezh.GetRegister(0) == expected
        assert keyboard._ezh.GetRegister(3) == 0
        assert keyboard.state().stopped_reason is None


def test_ezh_conditional_shift_or_accumulates_active_low_scan_bits(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus = keyboard._bus
        code = 0x20028000
        words = [
            (1 << 18) | (5 << 10) | (1 << 20),  # R5 = one-bit mask
            # Test a low input: AZ is true, so accumulate shifted R5 into R0.
            0x18 | (2 << 10) | (2 << 14) | (5 << 29) | 512,
            0x41500030,  # scanner's conditional LSL_OR(AZ, R0, R0, R5, 1)
            (1 << 18) | (2 << 10) | (1 << 20),
            # A high input clears AZ, suppressing the next whole instruction.
            0x18 | (2 << 10) | (2 << 14) | (5 << 29) | 512,
            0x42500030,
        ]
        for index, word in enumerate(words):
            bus.WriteDoubleWord(code + index * 4, word)
        bus.WriteDoubleWord(0x4001D020, code)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        keyboard._ezh.AdvanceCycles(len(words))
        assert keyboard._ezh.GetRegister(0) == 2
        assert keyboard.state().stopped_reason is None


def test_ezh_fused_add_sets_arithmetic_carry_and_zero_flags(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        bus = keyboard._bus
        code = 0x20028000
        words = [
            (1 << 18) | (1 << 10) | (2 << 20),  # R1 = 2
            (1 << 18) | (2 << 10) | (0x7FF << 20),  # R2 = -1
            0x10 | (1 << 14) | (2 << 20) | (1 << 24) | (4 << 29) | 512,
            # (0xFFFFFFFF << 1) + 2 = zero, with arithmetic carry.
            (1 << 18) | (3 << 10) | (7 << 20) | (9 << 5),  # ACZ
        ]
        for index, word in enumerate(words):
            bus.WriteDoubleWord(code + index * 4, word)
        bus.WriteDoubleWord(0x4001D020, code)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        keyboard._ezh.AdvanceCycles(len(words))
        assert keyboard._ezh.GetRegister(0) == 0
        assert keyboard._ezh.GetRegister(3) == 7
        assert keyboard.state().stopped_reason is None


def test_flash_execution_reprogramming_and_boot_reset_preserve_nonvolatile_state(cpu_image):
    import hashlib

    from razer_emu import Bootloader

    image = bytearray(0x8000)
    struct.pack_into("<II", image, 0, 0x04008000, 0x101)
    image[0x100:0x106] = bytes.fromhex("2a200131fde7")  # movs r0,42; adds r1,1; b 0x102
    boot = Bootloader(bytes(image), 0x04008000, 0x101, hashlib.sha256(image).hexdigest())
    with Keyboard(Firmware.load(cpu_image), bootloader=boot) as keyboard:
        from System import Array, Byte

        # Cold boot starts in flash and leaves the SRAM application uncopied.
        assert keyboard.state().pc == 0x100
        assert keyboard.read_memory(0x20000000, 16) == bytes(16)
        assert keyboard.run_for(10).stopped_reason is None
        assert keyboard._value(keyboard._cpu.GetRegister(0)) == 42
        flash = keyboard._machine.sysbus.flash.internal
        flash.Erase(0, 512)
        image[0x100] = 99
        assert flash.Program(0, Array[Byte](image[:512]))
        keyboard._cpu.PC = keyboard._register(0x101)
        assert keyboard.run_for(10).stopped_reason is None
        assert keyboard._value(keyboard._cpu.GetRegister(0)) == 99
        keyboard.reset()
        assert keyboard.read_memory(0x100, 2) == bytes([99, 0x20])
        assert keyboard.run_for(10).stopped_reason is None
        assert keyboard._value(keyboard._cpu.GetRegister(0)) == 99


@pytest.mark.parametrize("fetch", [False, True])
def test_executable_flash_preserves_ecc_faults(cpu_image, fetch):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        from System import Array, Byte

        # Exercise both data reads and instruction fetch from an ECC hole.
        code = bytes.fromhex("01480168fee700bf00400400") + bytes(4)
        keyboard._machine.sysbus.flash.internal.LoadImage(0x100, Array[Byte](code))
        keyboard._cpu.PC = keyboard._register(0x44001 if fetch else 0x101)
        state = keyboard.run_for(10)
        assert state.stopped_reason == "cpu_fault"
        assert keyboard._bus.ReadDoubleWord(0x40034FE0) & 8
        if not fetch:
            assert state.fault_status & 0x8200 == 0x8200
            assert state.fault_address == 0x44000


def test_private_complete_bootloader_cold_boots_original_application(cpu_image):
    from razer_emu import Bootloader

    path = os.environ.get("RAZER_TEST_FIRMWARE")
    dumps = os.environ.get("RAZER_TEST_FLASH_DUMPS")
    boot_dump = os.environ.get("RAZER_TEST_BOOTLOADER_DUMP")
    if not path or not dumps or not boot_dump:
        pytest.skip("Set private firmware, flash captures and complete bootloader capture")
    firmware = Firmware.load(path)
    boot = Bootloader.load_dump(boot_dump)
    regions = [FlashRegion.load_dump(p) for p in dumps.split(os.pathsep)]
    with Keyboard(firmware, flash_regions=regions, bootloader=boot) as keyboard:
        assert keyboard.state().pc == boot.reset_vector & ~1
        assert keyboard.read_memory(0x20000000, len(firmware.image)) == bytes(len(firmware.image))
        for _ in range(2):
            enumeration = keyboard.usb.enumerate_device()
            assert struct.unpack_from("<HH", enumeration.device, 8) == (0x1532, 0x02A5)
            assert enumeration.configuration[4] == 5
            assert keyboard.read_memory(0x20000000, len(firmware.image)) == firmware.image
            assert keyboard.state().stopped_reason is None
            # Neither application handoff nor direct memory mapping is injected.
            # The actual bootloader copies the application and selects its vectors.
            assert keyboard._cpu.VectorTableOffset == 0x20000000
            keyboard.reset()
            assert keyboard.state().pc == boot.reset_vector & ~1
            assert keyboard.read_memory(0x20000000, 16) == bytes(16)
