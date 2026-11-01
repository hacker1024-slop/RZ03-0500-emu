"""Lighting register behavior reached through the MCU I2C controller."""

import os

import pytest

from razer_emu import Firmware, FlashRegion
from razer_emu.core import Keyboard

I2C = 0x4008A800


def _write(bus, register, *data):
    bus.WriteDoubleWord(I2C, 1)
    bus.WriteDoubleWord(I2C + 0x28, 0xA0)
    bus.WriteDoubleWord(I2C + 0x20, 2)
    assert bus.ReadDoubleWord(I2C + 4) == 5
    for value in (register, *data):
        bus.WriteDoubleWord(I2C + 0x28, value)
        bus.WriteDoubleWord(I2C + 0x20, 1)
    bus.WriteDoubleWord(I2C + 0x20, 4)


def _read(bus, register):
    # Select a register and use a repeated START for the read, not a STOP.
    bus.WriteDoubleWord(I2C, 1)
    bus.WriteDoubleWord(I2C + 0x28, 0xA0)
    bus.WriteDoubleWord(I2C + 0x20, 2)
    bus.WriteDoubleWord(I2C + 0x28, register)
    bus.WriteDoubleWord(I2C + 0x20, 1)
    bus.WriteDoubleWord(I2C + 0x28, 0xA1)
    bus.WriteDoubleWord(I2C + 0x20, 2)
    result = bus.ReadDoubleWord(I2C + 0x28)
    bus.WriteDoubleWord(I2C + 0x20, 4)
    return result


def _page(bus, page):
    _write(bus, 0xFE, 0xC5)
    _write(bus, 0xFD, page)


def test_lighting_page_lock_pwm_enable_current_and_shutdown(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        chip = keyboard.attach_lighting(4, 0x50)
        bus = keyboard._bus
        assert chip.state().shutdown
        _write(bus, 0xFD, 1)  # locked: still page 0
        _write(bus, 0, 1)
        assert chip.state().pwm[0] == 0
        _page(bus, 1)
        assert _read(bus, 0xFE) == 0  # one-time unlock consumed by FD
        _write(bus, 0, 128, 255)
        _write(bus, 191, 64)
        _write(bus, 0xFD, 3)  # locked: must not change page
        _write(bus, 2, 42)
        assert chip.state().pwm[2] == 42
        _page(bus, 0)
        _write(bus, 0, 3)
        _write(bus, 23, 0x80)
        _page(bus, 3)
        _write(bus, 1, 128)
        _write(bus, 0, 1)
        snapshot = chip.state()
        assert snapshot.pwm[:3] == bytes((128, 255, 42))
        assert snapshot.levels[0] == pytest.approx(128 * 128 / 65025)
        assert snapshot.levels[1] == pytest.approx(128 / 255)
        assert snapshot.levels[2] == 0
        assert snapshot.levels[191] == pytest.approx(64 * 128 / 65025)
        _write(bus, 0, 0)
        assert not any(chip.state().levels)
        assert snapshot.levels[0] > 0  # frontend snapshots are immutable copies
        assert keyboard.state().stopped_reason is None


def test_lighting_read_reset_and_keyboard_reset_preserve_attachment(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        chip = keyboard.attach_lighting(4, 0x50)
        bus = keyboard._bus
        _page(bus, 1)
        _write(bus, 0, 99)
        _page(bus, 3)
        assert _read(bus, 0x11) == 0
        assert chip.state().pwm == bytes(192)
        assert chip.state().enabled == bytes(24)
        assert chip.state().shutdown and chip.state().global_current == 0
        _page(bus, 1)
        _write(bus, 0, 88)
        keyboard.reset()
        assert chip.state().pwm == bytes(192)
        assert _read(bus, 0xFE) == 0
        assert keyboard.state().stopped_reason is None


def test_lighting_unsupported_operations_are_sticky_until_core_reset(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        chip = keyboard.attach_lighting(4, 0x50)
        bus = keyboard._bus
        _page(bus, 3)
        _write(bus, 0, 3)  # automatic breathing has no timing model yet
        assert keyboard.state().stopped_reason == "unsupported_lighting"
        assert "breathing" in chip.state().unsupported_operation
        with pytest.raises(RuntimeError, match="breathing"):
            _ = chip.state().levels
        assert _read(bus, 0x11) == 0
        assert keyboard.state().stopped_reason == "unsupported_lighting"
        assert chip.state().unsupported_operation
        keyboard.reset()
        assert keyboard.state().stopped_reason is None
        assert chip.state().unsupported_operation is None


def test_lighting_configuration_validation_and_lifetime(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        for bus, address in ((1, 0x50), (True, 0x50), (4, 0x60), (4, True)):
            with pytest.raises(ValueError):
                keyboard.attach_lighting(bus, address)
        chip = keyboard.attach_lighting(4, 0x50)
        with pytest.raises(ValueError, match="already"):
            keyboard.attach_lighting(4, 0x50)
        keyboard.run_for(1)
        with pytest.raises(RuntimeError, match="before"):
            keyboard.attach_lighting(4, 0x51)
        keyboard.close()
        with pytest.raises(RuntimeError, match="closed"):
            chip.state()


def test_private_firmware_initializes_configured_lighting_controller(cpu_image):
    path = os.environ.get("RAZER_TEST_FIRMWARE")
    dumps = os.environ.get("RAZER_TEST_FLASH_DUMPS")
    if not path or not dumps:
        pytest.skip("Set private firmware and verified captures for lighting integration")
    regions = [FlashRegion.load_dump(p) for p in dumps.split(os.pathsep)]
    with Keyboard(Firmware.load(path), flash_regions=regions) as keyboard:
        chip = keyboard.attach_lighting(4, 0x50)
        keyboard.usb.enumerate_device()
        # Initialization is interleaved with probes of other I2C addresses.
        # Wait for the observed enable-byte write instead of assuming a boot time.
        for _ in range(50):
            assert keyboard.run_for(10_000).stopped_reason is None
            snapshot = chip.state()
            if snapshot.enabled == bytes([255]) * 24:
                break
        assert not snapshot.shutdown and snapshot.global_current == 255
        assert snapshot.enabled == bytes([255]) * 24
        assert len(snapshot.pwm) == 192
        assert snapshot.unsupported_operation is None


def test_i2c_dma_read_does_not_prefetch_past_last_lighting_register(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        chip = keyboard.attach_lighting(4, 0x50)
        bus = keyboard._bus
        _write(bus, 0xFE, 0xC5)
        # Select FE, then repeated START for a single-byte DMA read. FF is
        # unsupported, so a speculative extra read would stop the emulator.
        bus.WriteDoubleWord(I2C + 0x28, 0xA0)
        bus.WriteDoubleWord(I2C + 0x20, 2)
        bus.WriteDoubleWord(I2C + 0x28, 0xFE)
        bus.WriteDoubleWord(I2C + 0x20, 1)
        bus.WriteDoubleWord(I2C + 0x28, 0xA1)
        bus.WriteDoubleWord(I2C + 0x20, 10)
        table, target, channel = 0x2002A000, 0x2002A400, 12
        for offset, value in ((4, I2C + 0x28), (8, target), (12, 0)):
            bus.WriteDoubleWord(table + channel * 16 + offset, value)
        dma = 0x40082000
        bus.WriteDoubleWord(dma + 8, table)
        bus.WriteDoubleWord(dma, 1)
        bus.WriteDoubleWord(dma + 0x20, 1 << channel)
        bus.WriteDoubleWord(dma + 0x400 + channel * 16, 1)
        bus.WriteDoubleWord(dma + 0x408 + channel * 16, 0x4015)
        assert keyboard.run_for(1).stopped_reason is None
        assert keyboard.read_memory(target, 1) == b"\xc5"
        assert chip.state().unsupported_operation is None
        bus.WriteDoubleWord(I2C + 0x20, 4)
