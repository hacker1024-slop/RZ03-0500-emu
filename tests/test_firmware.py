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
