"""Photo hit regions, electrical maps and LED channel interpretation."""

import os

import pytest

from razer_emu import Firmware, LightingState
from razer_emu_gui.layout import key_colors, load_layout


def test_ansi_layout_has_unique_contacts_and_photo_hit_regions():
    layout = load_layout()
    assert len(layout["keys"]) == 83
    assert len({k["key_id"] for k in layout["keys"]}) == 83
    contacts = [tuple(k["matrix"]) for k in layout["keys"] if k["matrix"] is not None]
    assert len(contacts) == len(set(contacts)) == 81
    assert sorted(k["button"] for k in layout["keys"] if k["button"] is not None) == [0, 1]
    for key in layout["keys"]:
        assert key["width"] > 0 and key["height"] > 0
        assert 0 <= key["x"] < key["x"] + key["width"] <= layout["width"]
        assert 0 <= key["y"] < key["y"] + key["height"] <= layout["height"]
        assert 0 <= key["controller"] < len(layout["controllers"])
        assert len(set(key["rgb"])) == 3 and all(0 <= c < 192 for c in key["rgb"])


def test_key_rgb_respects_channel_order_current_enable_and_shutdown():
    layout = load_layout()
    key = next(k for k in layout["keys"] if k["label"] == "A")
    assert key["matrix"] == [3, 1]
    assert key["rgb"] == [161, 145, 177]
    pwm = bytearray(192)
    for channel, value in zip(key["rgb"], (255, 128, 64)):
        pwm[channel] = value
    healthy = LightingState(bytes(pwm), b"\xff" * 24, 128, False)
    others = LightingState(bytes(192), bytes(24), 255, False)
    assert key_colors(layout, [healthy, others, others])[key["id"]] == "#804020"
    disabled = bytearray(b"\xff" * 24)
    channel = key["rgb"][1]
    disabled[channel // 8] &= ~(1 << (channel % 8))
    masked = LightingState(bytes(pwm), bytes(disabled), 128, False)
    assert key_colors(layout, [masked, others, others])[key["id"]] == "#800020"
    off = LightingState(bytes(pwm), b"\xff" * 24, 128, True)
    assert key_colors(layout, [off, others, others])[key["id"]] == "#000000"


def test_private_firmware_matches_every_gui_contact_and_rgb_record():
    path = os.environ.get("RAZER_TEST_FIRMWARE")
    if not path:
        pytest.skip("Set private firmware to verify the recovered map")
    firmware = Firmware.load(path)
    layout = load_layout()
    assert firmware.sha256 == layout["firmware_sha256"]
    scan = firmware.image[0x14F30:0x14FB0]
    for key in layout["keys"]:
        if key["matrix"] is not None:
            row, col = key["matrix"]
            assert scan[row * 16 + col] == key["key_id"]
        row, col = key["led_slot"]
        offset = 0x15772 + (row * 18 + col) * 7
        record = firmware.image[offset : offset + 7]
        assert record[0] == key["key_id"]
        assert record[3] == key["controller"]
        assert list(record[4:]) == key["rgb"]
