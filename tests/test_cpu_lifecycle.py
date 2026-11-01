"""Focused regressions for quiescent CPU thread reuse, using real Thumb ISRs."""

import struct
import time
from pathlib import Path

import pytest

from razer_emu import Firmware
from razer_emu.core import Keyboard

pytestmark = pytest.mark.skipif(
    not Path("/proc/self/task").is_dir(), reason="CPU thread-identity checks require Linux procfs"
)


def cpu_threads(keyboard):
    result = set()
    for path in Path("/proc/self/task").iterdir():
        try:
            if (path / "comm").read_text().strip() == keyboard._name[:15]:
                result.add(int(path.name))
        except FileNotFoundError:
            pass
    return result


def counter(keyboard):
    return struct.unpack("<I", keyboard.read_memory(0x20001008, 4))[0]


def assert_quiescent(keyboard):
    assert keyboard._cpu.IsPaused and keyboard._ezh_cpu.IsPaused
    before = keyboard.state().to_dict()
    cycles = keyboard._ezh.Cycles
    time.sleep(0.02)
    assert keyboard.state().to_dict() == before
    assert keyboard._ezh.Cycles == cycles


def test_irq_asserted_while_parked_reaches_real_thumb_handler(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        keyboard.run_for(1000)
        threads = cpu_threads(keyboard)
        assert len(threads) == 2
        bus = keyboard._bus
        # Stop periodic generation and drain any handler already in progress.
        bus.WriteDoubleWord(0x40028004, 0)
        bus.WriteDoubleWord(0x40028000, 0xF)
        bus.WriteDoubleWord(0xE000E284, 1 << 4)
        keyboard.run_for(10)
        previous = counter(keyboard)
        assert_quiescent(keyboard)
        # IRQ 36 arrives with both native CPU threads parked. TranslationCPU
        # must still pass the edge through native SetIrq while started.
        keyboard._machine.sysbus.ctimer2.internal.IRQ.Set(True)
        assert bus.ReadDoubleWord(0xE000E204) & (1 << 4)
        assert counter(keyboard) == previous
        assert keyboard.run_for(10).stopped_reason is None
        assert counter(keyboard) == previous + 1
        assert cpu_threads(keyboard) == threads
        assert_quiescent(keyboard)
    assert cpu_threads(keyboard) == set()


def test_fault_after_park_stops_and_reset_restarts_and_close_joins(cpu_image):
    keyboard = Keyboard(Firmware.load(cpu_image))
    try:
        keyboard.run_for(1000)
        assert len(cpu_threads(keyboard)) == 2
        # Inject a Thumb load of unsupported MMIO after a completed bounded run.
        from System import Array, Byte

        code = 0x20000200
        keyboard._bus.WriteBytes(Array[Byte](bytes.fromhex("01480168fee700bf00f00f40")), code)
        keyboard._cpu.PC = keyboard._register(code | 1)
        before = keyboard.state().elapsed_us
        state = keyboard.run_for(100)
        assert state.stopped_reason == "unsupported_mmio"
        assert state.elapsed_us == before + 10
        assert_quiescent(keyboard)
        with pytest.raises(RuntimeError, match="reset"):
            keyboard.run_for(10)
        keyboard.reset()
        assert not cpu_threads(keyboard)
        assert keyboard.run_for(1000).stopped_reason is None
        assert 8 <= counter(keyboard) <= 10
        assert_quiescent(keyboard)
    finally:
        keyboard.close()
    assert cpu_threads(keyboard) == set()


def test_ezh_breakpoint_written_while_parked_takes_effect(cpu_image):
    image = bytearray(Firmware.load(cpu_image).image)
    struct.pack_into("<I", image, 4, 0x20000141)
    image[0x140:0x142] = bytes.fromhex("fee7")
    with Keyboard(Firmware.from_bytes(image)) as keyboard:
        bus, ezh = keyboard._bus, keyboard._ezh
        code, handler = 0x20028000, 0x20028100
        def jump(address):
            return 0x15 | 512 | ((address & 0x7FFFFC) << 9)

        bus.WriteDoubleWord(code, 0x00140006)
        bus.WriteDoubleWord(code + 4, jump(code))
        bus.WriteDoubleWord(handler, (1 << 10) | (1 << 18) | (42 << 20))
        bus.WriteDoubleWord(handler + 4, jump(handler + 4))
        bus.WriteDoubleWord(0x4001D020, code)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        keyboard.run_for(10)
        threads = cpu_threads(keyboard)
        assert ezh.GetRegister(1) == 0
        bus.WriteDoubleWord(0x4001D030, code)
        bus.WriteDoubleWord(0x4001D034, handler)
        assert_quiescent(keyboard)
        assert keyboard.run_for(10).stopped_reason is None
        assert ezh.GetRegister(1) == 42
        assert ezh.GetRegister(13) == handler + 4
        assert cpu_threads(keyboard) == threads
        assert_quiescent(keyboard)
    assert cpu_threads(keyboard) == set()
