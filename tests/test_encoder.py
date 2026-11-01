"""Encoder contacts share the CPU GPIO receiver, including pull and reset behavior."""

import pytest

from razer_emu import Firmware
from razer_emu.core import Keyboard


def test_encoder_phases_reach_gpio_aliases_and_survive_reset(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        assert keyboard.encoder is None
        encoder = keyboard.attach_encoder()
        assert keyboard.encoder is encoder
        assert encoder.phases == (True, True)
        for phase_a, phase_b in ((True, False), (False, False), (False, True), (True, True)):
            encoder.set_phases(phase_a, phase_b)
            # Reapplying firmware pull settings cannot override contact levels.
            keyboard._bus.WriteDoubleWord(0x40001088, 0x120)
            keyboard._bus.WriteDoubleWord(0x4000108C, 0x120)
            assert keyboard._bus.ReadByte(0x4008C022) == phase_a
            assert keyboard._bus.ReadByte(0x4008C023) == phase_b
            assert keyboard._bus.ReadDoubleWord(0x4008D088) == phase_a
            assert keyboard._bus.ReadDoubleWord(0x4008D08C) == phase_b
        encoder.set_phases(False, True)
        snapshot = encoder.phases
        keyboard.reset()
        assert encoder.phases == snapshot == (False, True)
        assert keyboard._bus.ReadByte(0x4008C022) == 0
        assert keyboard._bus.ReadByte(0x4008C023) == 1
        with pytest.raises(ValueError, match="already"):
            keyboard.attach_encoder()
    with pytest.raises(RuntimeError, match="closed"):
        encoder.set_phases(True, True)
    with pytest.raises(RuntimeError, match="closed"):
        _ = encoder.phases


def test_encoder_attachment_and_phase_validation(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        with pytest.raises(ValueError, match="booleans"):
            keyboard.attach_encoder(phase_a=1)
        assert keyboard.encoder is None
        encoder = keyboard.attach_encoder(phase_a=False, phase_b=True)
        for phases in ((0, True), (False, None), ("high", False)):
            with pytest.raises(ValueError, match="booleans"):
                encoder.set_phases(*phases)
            assert encoder.phases == (False, True)
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        keyboard.run_for(10)
        with pytest.raises(RuntimeError, match="before firmware"):
            keyboard.attach_encoder()
