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


def _boot_capture(directory, data, mask=None, address=0):
    import json

    mask = bytes([1] * len(data)) if mask is None else mask
    (directory / "bootloader.bin").write_bytes(data)
    (directory / "valid-mask.bin").write_bytes(mask)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "state": "complete_with_holes" if not all(mask) else "complete",
                "image_file": "bootloader.bin",
                "verified_bytes": sum(mask),
                "stable_bytes": sum(mask),
                "region": {"start": address, "length": len(data)},
                "sha256": hashlib.sha256(data).hexdigest(),
                "validity_mask_sha256": hashlib.sha256(mask).hexdigest(),
            }
        )
    )


def test_bootloader_requires_complete_verified_capture(tmp_path):
    from razer_emu import Bootloader

    image = struct.pack("<II", 0x04008000, 0x215) + bytes(0x8000 - 8)
    _boot_capture(tmp_path, image)
    boot = Bootloader.load_dump(tmp_path)
    assert boot.image == image
    assert (boot.initial_sp, boot.reset_vector) == (0x04008000, 0x215)
    assert boot.sha256 == hashlib.sha256(image).hexdigest()
    _boot_capture(tmp_path, image, bytes([1] * 1024 + [0] * (len(image) - 1024)))
    with pytest.raises(ValueError, match="fully verified"):
        Bootloader.load_dump(tmp_path)


@pytest.mark.parametrize(
    "sp,reset,size,address",
    [
        (0x04008000, 0x214, 0x8000, 0),
        (0x04008000, 0x8001, 0x8000, 0),
        (0x04008000, 0x20000215, 0x8000, 0),
        (0x10000000, 0x215, 0x8000, 0),
        (0x04008000, 0x215, 0x4000, 0),
        (0x04008000, 0x215, 0x8000, 0x8000),
    ],
)
def test_bootloader_rejects_invalid_vectors_and_regions(tmp_path, sp, reset, size, address):
    from razer_emu import Bootloader

    _boot_capture(tmp_path, struct.pack("<II", sp, reset) + bytes(size - 8), address=address)
    with pytest.raises(ValueError):
        Bootloader.load_dump(tmp_path)
