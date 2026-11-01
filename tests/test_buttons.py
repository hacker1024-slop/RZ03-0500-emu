"""Independent contacts use native Renode buttons and the board GPIO receiver."""

import pytest

from razer_emu import Firmware
from razer_emu.buttons import BUTTON_PINS
from razer_emu.core import Keyboard


def test_buttons_are_active_low_and_preserved_across_reset(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        assert keyboard.buttons is None
        buttons = keyboard.attach_buttons()
        assert keyboard.buttons is buttons
        assert not buttons.pressed
        for pin in BUTTON_PINS:
            assert keyboard._bus.ReadByte(0x4008C000 + pin) == 1
        buttons.press(0)
        keyboard._bus.WriteDoubleWord(0x40001000 + BUTTON_PINS[0] * 4, 0x120)
        assert keyboard._bus.ReadByte(0x4008C000 + BUTTON_PINS[0]) == 0
        assert keyboard._bus.ReadByte(0x4008C000 + BUTTON_PINS[1]) == 1
        snapshot = buttons.pressed
        keyboard.reset()
        assert buttons.pressed == snapshot == frozenset({0})
        assert keyboard._bus.ReadByte(0x4008C000 + BUTTON_PINS[0]) == 0
        buttons.press(1)
        buttons.release(0)
        assert buttons.pressed == frozenset({1})
        buttons.release(1)
        assert not buttons.pressed
        with pytest.raises(ValueError, match="already"):
            keyboard.attach_buttons()
    with pytest.raises(RuntimeError, match="closed"):
        buttons.press(0)
    with pytest.raises(RuntimeError, match="closed"):
        _ = buttons.pressed


def test_buttons_validate_contacts_and_attachment_timing(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        buttons = keyboard.attach_buttons()
        for index, pressed in ((-1, True), (2, True), (True, True), (0, 1), (0, None)):
            with pytest.raises(ValueError):
                buttons.set_pressed(index, pressed)
            assert not buttons.pressed
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        keyboard.run_for(10)
        with pytest.raises(RuntimeError, match="before firmware"):
            keyboard.attach_buttons()
