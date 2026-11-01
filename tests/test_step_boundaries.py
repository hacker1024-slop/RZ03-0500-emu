import struct

import pytest

from razer_emu import Firmware
from razer_emu.core import Keyboard


def _counter_firmware(cpu_image):
    image = bytearray(Firmware.load(cpu_image).image)
    struct.pack_into("<I", image, 4, 0x20000141)
    # adds r0, #1; b 0x20000140 -- independent guest-visible instruction counter.
    image[0x140:0x144] = bytes.fromhex("0130fde7")
    return Firmware.from_bytes(image)


@pytest.mark.parametrize("count", [1, 95, 96, 97, 1000])
def test_step_then_minimum_timed_run_is_bounded(cpu_image, count):
    with Keyboard(_counter_firmware(cpu_image)) as keyboard:
        source = keyboard._engine.MasterTimeSource
        local = keyboard._machine.internal.LocalTimeSource
        quanta = (int(source.Quantum.Ticks), int(local.Quantum.Ticks))
        keyboard.step(count)
        assert keyboard._value(keyboard._cpu.GetRegister(0)) == (count + 1) // 2
        assert keyboard.state().elapsed_us == 0
        assert (int(source.Quantum.Ticks), int(local.Quantum.Ticks)) == quanta
        for _ in range(2):
            before_time = int(source.ElapsedVirtualTime.Ticks)
            before_instructions = int(keyboard._cpu.ExecutedInstructions)
            before_counter = keyboard._value(keyboard._cpu.GetRegister(0))
            state = keyboard.run_for(1)
            assert state.stopped_reason is None
            assert int(source.ElapsedVirtualTime.Ticks) - before_time == 1000
            # A stepped partial CPU tick may contribute previously executed
            # instructions. The timed run must never execute an extra quantum.
            assert 1 <= int(keyboard._cpu.ExecutedInstructions) - before_instructions <= 96
            assert keyboard._value(keyboard._cpu.GetRegister(0)) - before_counter <= 48


def test_repeated_steps_preserve_counts_then_run_exact_time(cpu_image):
    with Keyboard(_counter_firmware(cpu_image)) as keyboard:
        for count, expected in [(1, 1), (1, 1), (1000, 501)]:
            keyboard.step(count)
            assert keyboard._value(keyboard._cpu.GetRegister(0)) == expected
        before = int(keyboard._engine.MasterTimeSource.ElapsedVirtualTime.Ticks)
        state = keyboard.run_for(10)
        assert state.stopped_reason is None
        assert int(keyboard._engine.MasterTimeSource.ElapsedVirtualTime.Ticks) - before == 10000
        assert keyboard._value(keyboard._cpu.GetRegister(0)) == 978


def test_timed_run_step_reset_and_resume_keep_time_bounded(cpu_image):
    with Keyboard(_counter_firmware(cpu_image)) as keyboard:
        keyboard.run_for(19)
        before_steps = keyboard._value(keyboard._cpu.GetRegister(0))
        keyboard.step(1000)
        assert keyboard._value(keyboard._cpu.GetRegister(0)) == before_steps + 500
        assert keyboard.state().elapsed_us == 19
        for reset in (False, True):
            if reset:
                keyboard.step(1000)
                keyboard.reset()
                assert keyboard._value(keyboard._cpu.GetRegister(0)) == 0
            before = int(keyboard._engine.MasterTimeSource.ElapsedVirtualTime.Ticks)
            assert keyboard.run_for(1).stopped_reason is None
            assert int(keyboard._engine.MasterTimeSource.ElapsedVirtualTime.Ticks) - before == 1000
