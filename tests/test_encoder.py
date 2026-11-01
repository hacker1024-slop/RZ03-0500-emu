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


def test_private_firmware_reports_volume_and_mute_from_normal_inputs(cpu_image):
    import os
    import struct

    from razer_emu import FlashRegion

    path = os.environ.get("RAZER_TEST_FIRMWARE")
    dumps = os.environ.get("RAZER_TEST_FLASH_DUMPS")
    if not path or not dumps:
        pytest.skip("Set private 1.1.1 firmware and captures with default media bindings")
    firmware = Firmware.load(path)
    regions = [FlashRegion.load_dump(p) for p in dumps.split(os.pathsep)]
    with Keyboard(firmware, flash_regions=regions) as keyboard:
        keyboard.attach_matrix()
        encoder = keyboard.attach_encoder()
        buttons = keyboard.attach_buttons()
        keyboard.usb.enumerate_device()
        keyboard.run_for(5000)
        reports = []

        def sample(duration):
            for _ in range(duration // 125):
                assert keyboard.run_for(125).stopped_reason is None
                packet = keyboard.usb.poll_interrupt(0x82, 22)
                if packet is not None:
                    reports.append(packet)

        # This is ordinary firmware execution throughout: no entry overrides,
        # ready-flag writes, scanner replacement or prebuilt endpoint buffers.
        for phase_a, phase_b in ((1, 0), (0, 0), (0, 1), (1, 1), (0, 1), (0, 0), (1, 0), (1, 1)):
            encoder.set_phases(bool(phase_a), bool(phase_b))
            sample(5000)
        buttons.press(0)
        sample(40000)
        buttons.release(0)
        sample(40000)
        usages = (0xEA, 0, 0xEA, 0, 0xE9, 0, 0xE9, 0, 0xE2, 0)
        assert reports == [struct.pack("<BH", 2, usage) + bytes(19) for usage in usages]
        assert keyboard.read_memory(0x20000000, len(firmware.image)) == firmware.image
        assert keyboard.state().stopped_reason is None
