import hashlib
import struct

import pytest

from razer_emu import Firmware


def application(sp=0x04008000, reset=0x20000141):
    return struct.pack("<II", sp, reset) + bytes(0x200 - 8)


def test_load_validates_and_preserves_original_image(tmp_path):
    image = application()
    source = tmp_path / "application.bin"
    source.write_bytes(image)
    firmware = Firmware.load(source)
    assert firmware.image == image
    assert firmware.sha256 == hashlib.sha256(image).hexdigest()
    assert firmware.reset_vector == 0x20000141


@pytest.mark.parametrize(
    "image",
    [
        b"",
        bytes(0x40000),
        application(sp=0x10000000),
        application(sp=0x04007FFF),
        application(reset=0x20000140),
        application(reset=0x20030001),
    ],
)
def test_rejects_invalid_boot_state(image):
    with pytest.raises(ValueError):
        Firmware.from_bytes(image)


def test_flash_capture_hashes_and_validity_are_checked(tmp_path):
    import json

    from razer_emu import FlashRegion

    data = bytes(range(32))
    mask = bytes([1] * 16 + [0] * 16)
    (tmp_path / "settings.with-holes.bin").write_bytes(data)
    (tmp_path / "valid-mask.bin").write_bytes(mask)
    manifest = {
        "format_version": 1,
        "state": "complete_with_holes",
        "image_file": "settings.with-holes.bin",
        "verified_bytes": 16,
        "stable_bytes": 16,
        "region": {"start": 0x44000, "length": 32},
        "sha256": hashlib.sha256(data).hexdigest(),
        "validity_mask_sha256": hashlib.sha256(mask).hexdigest(),
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    region = FlashRegion.load_dump(tmp_path)
    assert region.address == 0x44000
    assert region.valid_mask == mask
    (tmp_path / "settings.with-holes.bin").write_bytes(bytes(32))
    with pytest.raises(ValueError, match="integrity"):
        FlashRegion.load_dump(tmp_path)
