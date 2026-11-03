"""Photo hit regions, electrical maps and LED channel interpretation."""

import os
import re

import pytest

from razer_emu import LightingState
from razer_emu_gui.layout import key_colors, load_layout
from razer_emu_gui.worker import load_images


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
    zone = layout["underglow"][0]
    pwm = bytearray(192)
    pwm[zone["rgb"][2]] = 255
    side = LightingState(bytes(pwm), b"\xff" * 24, 255, False)
    assert key_colors(layout, [others, others, side])[zone["id"]] == "#0000ff"



def test_private_firmware_matches_every_gui_contact_and_rgb_record():
    path = os.environ.get("RAZER_TEST_FIRMWARE")
    if not path:
        pytest.skip("Set private firmware to verify the recovered map")
    firmware, _, _ = load_images({"firmware": path, "flash_dumps": []})
    layout = load_layout()
    contacts = {}
    led_fields = {}
    for key in layout["keys"]:
        if key["matrix"] is not None:
            row, col = key["matrix"]
            contacts[row * 16 + col] = key["key_id"]
    assert len(layout["underglow"]) == 18
    for light in (*layout["keys"], *layout["underglow"]):
        row, col = light["led_slot"]
        offset = (row * 18 + col) * 7
        led_fields[offset] = light["key_id"]
        for index, value in enumerate([light["controller"], *light["rgb"]], start=3):
            led_fields[offset + index] = value

    for fields in (contacts, led_fields):
        pattern = b"".join(
            re.escape(bytes([fields[i]])) if i in fields else b"."
            for i in range(max(fields) + 1)
        )
        assert len(list(re.finditer(pattern, firmware.image, re.DOTALL))) == 1
