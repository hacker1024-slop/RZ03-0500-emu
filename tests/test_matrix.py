"""Diode-isolated contacts evaluated through the EZH digital input register."""

import pytest

from razer_emu import Firmware
from razer_emu.core import Keyboard
from razer_emu.matrix import COLUMN_PINS, ROW_PINS

ROW_MASK = sum(1 << pin for pin in ROW_PINS)
COLUMN_MASK = sum(1 << pin for pin in COLUMN_PINS)


def _drive(keyboard, output, direction):
    bus, ezh, code = keyboard._bus, keyboard._ezh, 0x20028000
    words = [
        1 | (1 << 18) | (8 << 10) | (13 << 14) | (62 << 24),
        1 | (1 << 18) | (9 << 10) | (13 << 14) | (62 << 24),
        14 << 14,  # MOV R0, GPI
    ]
    for index, word in enumerate(words):
        bus.WriteDoubleWord(code + index * 4, word)
    bus.WriteDoubleWord(code + 0x100, output)
    bus.WriteDoubleWord(code + 0x104, direction)
    bus.WriteDoubleWord(0x4001D024, 0xC0DE0010)
    bus.WriteDoubleWord(0x4001D020, code)
    bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
    ezh.AdvanceCycles(len(words))
    return int(ezh.GetRegister(0)) & COLUMN_MASK


def test_contact_is_visible_only_on_its_driven_low_row(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        matrix = keyboard.attach_matrix()
        assert _drive(keyboard, ROW_MASK, ROW_MASK) == COLUMN_MASK
        matrix.press(2, 5)
        snapshot = matrix.pressed
        assert _drive(keyboard, ROW_MASK, ROW_MASK) == COLUMN_MASK
        assert _drive(keyboard, 0, 0) == COLUMN_MASK  # rows are high impedance
        assert _drive(keyboard, ROW_MASK & ~(1 << ROW_PINS[1]), ROW_MASK) == COLUMN_MASK
        expected = COLUMN_MASK & ~(1 << COLUMN_PINS[5])
        assert _drive(keyboard, ROW_MASK & ~(1 << ROW_PINS[2]), ROW_MASK) == expected
        matrix.release(2, 5)
        assert keyboard._ezh.GetRegister(14) & COLUMN_MASK == COLUMN_MASK
        assert snapshot == frozenset({(2, 5)}) and not matrix.pressed
        assert keyboard.state().stopped_reason is None


def test_diodes_prevent_a_three_contact_rectangle_from_ghosting(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        matrix = keyboard.attach_matrix()
        for coordinate in ((0, 0), (0, 1), (1, 0)):
            matrix.press(*coordinate)
        value = _drive(keyboard, ROW_MASK & ~(1 << ROW_PINS[1]), ROW_MASK)
        assert value & (1 << COLUMN_PINS[0]) == 0
        assert value & (1 << COLUMN_PINS[1])  # no phantom contact at (1, 1)
        value = _drive(keyboard, ROW_MASK & ~(1 << ROW_PINS[0]), ROW_MASK)
        assert value & ((1 << COLUMN_PINS[0]) | (1 << COLUMN_PINS[1])) == 0


def test_held_contacts_survive_mcu_reset_and_views_obey_lifetime(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        matrix = keyboard.attach_matrix()
        matrix.press(7, 15)
        keyboard.reset()
        assert matrix.pressed == frozenset({(7, 15)})
        value = _drive(keyboard, ROW_MASK & ~(1 << ROW_PINS[7]), ROW_MASK)
        assert value & (1 << COLUMN_PINS[15]) == 0
        keyboard.close()
        with pytest.raises(RuntimeError, match="closed"):
            matrix.release(7, 15)


def test_matrix_configuration_and_coordinate_validation(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        matrix = keyboard.attach_matrix()
        with pytest.raises(ValueError, match="already"):
            keyboard.attach_matrix()
        for row, column in ((-1, 0), (8, 0), (0, 16), (True, 0), (0, 1.0)):
            with pytest.raises(ValueError):
                matrix.press(row, column)
        with pytest.raises(ValueError, match="boolean"):
            matrix.set_key(0, 0, 1)
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        keyboard.run_for(1)
        with pytest.raises(RuntimeError, match="before"):
            keyboard.attach_matrix()


def test_ezh_scanner_accumulates_pressed_columns_into_a_row_mask(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        matrix = keyboard.attach_matrix()
        for column in (0, 7, 15):
            matrix.press(5, column)
        bus, code = keyboard._bus, 0x20028000
        words = [
            1 | (1 << 18) | (8 << 10) | (13 << 14) | (126 << 24),
            1 | (1 << 18) | (9 << 10) | (13 << 14) | (126 << 24),
            1 << 18,  # R0 = 0
            (1 << 18) | (5 << 10) | (1 << 20),  # R5 = 1
        ]
        for column, pin in enumerate(COLUMN_PINS):
            words.append(0x18 | (14 << 10) | (14 << 14) | (pin << 24) | (5 << 29) | 512)
            words.append(0x10 | (1 << 5) | (5 << 20) | (column << 24) | (2 << 29))
        for index, word in enumerate(words):
            bus.WriteDoubleWord(code + index * 4, word)
        bus.WriteDoubleWord(code + 0x200, ROW_MASK & ~(1 << ROW_PINS[5]))
        bus.WriteDoubleWord(code + 0x204, ROW_MASK)
        bus.WriteDoubleWord(0x4001D020, code)
        bus.WriteDoubleWord(0x4001D024, 0xC0DE0011)
        keyboard._ezh.AdvanceCycles(len(words))
        assert keyboard._ezh.GetRegister(0) == 0x8081
        assert keyboard.state().stopped_reason is None
