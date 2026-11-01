import struct

import pytest

from razer_emu import Firmware
from razer_emu.core import Keyboard


@pytest.fixture
def loop_image(cpu_image):
    image = bytearray(Firmware.load(cpu_image).image)
    struct.pack_into("<I", image, 4, 0x20000141)
    image[0x140:0x142] = bytes.fromhex("fee7")
    return Firmware.from_bytes(image)


def configure_watchdog(keyboard, timeout=0xFF, enabled=True):
    bus = keyboard._bus
    base = 0x4000C000
    bus.WriteDoubleWord(base + 4, timeout)
    bus.WriteDoubleWord(base, 1 if enabled else 0)
    feed_watchdog(keyboard)


def feed_watchdog(keyboard):
    keyboard._bus.WriteDoubleWord(0x4000C008, 0xAA)
    keyboard._bus.WriteDoubleWord(0x4000C008, 0x55)


def test_watchdog_extension_reschedules_at_old_deadline(loop_image):
    with Keyboard(loop_image) as keyboard:
        configure_watchdog(keyboard)
        assert keyboard.run_for(900).stopped_reason is None
        feed_watchdog(keyboard)
        assert keyboard.run_for(200).stopped_reason is None
        assert keyboard._bus.ReadDoubleWord(0x4000C00C) == 205
        assert keyboard.run_for(810).stopped_reason is None
        assert keyboard.run_for(10).stopped_reason == "watchdog_expired"


def test_watchdog_shorter_timeout_replaces_later_scheduled_event(loop_image):
    with Keyboard(loop_image) as keyboard:
        configure_watchdog(keyboard, timeout=4096)
        keyboard.run_for(100)
        keyboard._bus.WriteDoubleWord(0x4000C004, 0xFF)
        feed_watchdog(keyboard)
        assert keyboard.run_for(1010).stopped_reason is None
        assert keyboard.run_for(10).stopped_reason == "watchdog_expired"


@pytest.mark.parametrize("window,expired", [(249, True), (250, False), (251, False)])
def test_watchdog_window_includes_exact_boundary(loop_image, window, expired):
    with Keyboard(loop_image) as keyboard:
        configure_watchdog(keyboard)
        keyboard.run_for(20)
        assert keyboard._bus.ReadDoubleWord(0x4000C00C) == 250
        keyboard._bus.WriteDoubleWord(0x4000C018, window)
        feed_watchdog(keyboard)
        assert keyboard._watchdog.Expired == expired


def test_watchdog_feed_retains_fractional_divider_phase(loop_image):
    with Keyboard(loop_image) as keyboard:
        configure_watchdog(keyboard)
        keyboard.run_for(1)
        feed_watchdog(keyboard)
        keyboard.run_for(3)
        assert keyboard._bus.ReadDoubleWord(0x4000C00C) == 254
        assert keyboard.run_for(1015).stopped_reason is None
        assert keyboard.run_for(1).stopped_reason == "watchdog_expired"


@pytest.mark.parametrize("sequence", [[0x55], [0xAA, 0], [0xAA, 0xAA, 0]])
def test_invalid_feed_does_not_extend_deadline(loop_image, sequence):
    with Keyboard(loop_image) as keyboard:
        configure_watchdog(keyboard)
        keyboard.run_for(900)
        for value in sequence:
            keyboard._bus.WriteDoubleWord(0x4000C008, value)
        assert keyboard.run_for(120).stopped_reason == "watchdog_expired"


@pytest.mark.parametrize("timeout", [0, 1, 254, 255])
def test_watchdog_minimum_timeout_matches_reference_model(loop_image, timeout):
    with Keyboard(loop_image) as keyboard:
        configure_watchdog(keyboard, timeout=timeout)
        assert keyboard._bus.ReadDoubleWord(0x4000C00C) == 255
        assert keyboard.run_for(1010).stopped_reason is None
        assert keyboard.run_for(10).stopped_reason == "watchdog_expired"


def test_watchdog_disabled_feed_does_not_start_timer(loop_image):
    with Keyboard(loop_image) as keyboard:
        configure_watchdog(keyboard, enabled=False)
        keyboard._bus.WriteDoubleWord(0x4000C000, 1)
        assert keyboard.run_for(1500).stopped_reason is None
        assert keyboard._bus.ReadDoubleWord(0x4000C00C) == 255
        feed_watchdog(keyboard)
        assert keyboard.run_for(1020).stopped_reason == "watchdog_expired"


def test_watchdog_enable_is_sticky_and_reset_cancels_deadline(loop_image):
    with Keyboard(loop_image) as keyboard:
        configure_watchdog(keyboard)
        keyboard.run_for(100)
        keyboard._bus.WriteDoubleWord(0x4000C000, 0)
        assert keyboard._bus.ReadDoubleWord(0x4000C000) & 1
        feed_watchdog(keyboard)
        keyboard.reset()
        assert keyboard.run_for(1500).stopped_reason is None
        assert keyboard._bus.ReadDoubleWord(0x4000C00C) == 0xFFFFFF


def test_ezh_does_not_run_ahead_during_arm_debug_step(loop_image):
    with Keyboard(loop_image) as keyboard:
        code = 0x20028000
        keyboard._bus.WriteDoubleWord(code, 0x00140006)  # add r0,r0,#1
        keyboard._bus.WriteDoubleWord(code + 4, 0x15 | 512 | ((code & 0x7FFFFC) << 9))
        keyboard._bus.WriteDoubleWord(0x4001D020, code)
        keyboard._bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        keyboard.step()
        assert keyboard._ezh.Cycles == 0
        for _ in range(12):
            keyboard.step(1000)
            ticks = int(keyboard._machine.internal.ClockSource.CurrentValue.Ticks)
            assert keyboard._ezh.Cycles == ticks // 1000 * 96
        before = keyboard._ezh.Cycles
        assert keyboard.run_for(100).stopped_reason is None
        assert keyboard._ezh.Cycles == before + 9600
        keyboard.reset()
        assert keyboard._ezh.Cycles == keyboard._ezh.Instructions == 0
        assert keyboard._machine.sysbus.ezhCpu.internal.ExecutedInstructions == 0
