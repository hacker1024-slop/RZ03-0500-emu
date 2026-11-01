"""Validate external firmware and describe the bootloader's application handoff."""

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path

RUNTIME_BASE = 0x20000000
FLASH_APPLICATION = 0x8000
SRAM_SIZE = 0x30000
SRAMX_BASE = 0x04000000
SRAMX_SIZE = 0x8000


@dataclass(frozen=True)
class Firmware:
    image: bytes
    initial_sp: int
    reset_vector: int
    sha256: str

    @classmethod
    def load(cls, path):
        return cls.from_bytes(Path(path).read_bytes())

    @classmethod
    def from_bytes(cls, image):
        image = bytes(image)
        if not 0x140 <= len(image) <= SRAM_SIZE:
            raise ValueError("Expected a raw application image, including its vector table")
        sp, reset = struct.unpack_from("<II", image)
        if sp % 8 or not (
            SRAMX_BASE < sp <= SRAMX_BASE + SRAMX_SIZE
            or RUNTIME_BASE < sp <= RUNTIME_BASE + SRAM_SIZE
        ):
            raise ValueError("Initial stack pointer is outside LPC5528 SRAM")
        if not reset & 1 or not RUNTIME_BASE <= (reset & ~1) < RUNTIME_BASE + len(image):
            raise ValueError("Expected a Thumb reset vector inside the SRAM application")
        return cls(image, sp, reset, hashlib.sha256(image).hexdigest())


@dataclass(frozen=True)
class FlashRegion:
    """An external flash capture with byte validity; no guessed contents."""

    address: int
    data: bytes
    valid_mask: bytes

    def __post_init__(self):
        object.__setattr__(self, "data", bytes(self.data))
        object.__setattr__(self, "valid_mask", bytes(self.valid_mask))
        if (
            not isinstance(self.address, int)
            or self.address < 0
            or self.address % 16
            or not self.data
            or len(self.data) % 16
            or self.address + len(self.data) > 0x80000
        ):
            raise ValueError("Flash regions must contain aligned 16-byte words within flash")
        if len(self.valid_mask) != len(self.data) or any(n not in (0, 1) for n in self.valid_mask):
            raise ValueError("Expected one 0/1 validity byte per captured byte")

    @classmethod
    def load_dump(cls, directory):
        """Load a verified razer-firmware-dump output, preserving unreadable holes."""
        import json

        directory = Path(directory)
        manifest = json.loads((directory / "manifest.json").read_text())
        try:
            name = manifest["image_file"]
            if (
                manifest["format_version"] != 1
                or manifest["state"] not in ("complete", "complete_with_holes")
                or manifest["verified_bytes"] != manifest["stable_bytes"]
                or Path(name).name != name
            ):
                raise ValueError("Expected a completed, verified firmware-dump capture")
            image_path = directory / name
            mask_path = directory / "valid-mask.bin"
            if image_path.stat().st_size > 0x80000 or mask_path.stat().st_size > 0x80000:
                raise ValueError("Capture exceeds the flash bank")
            data, mask = image_path.read_bytes(), mask_path.read_bytes()
            if (
                hashlib.sha256(data).hexdigest() != manifest["sha256"]
                or hashlib.sha256(mask).hexdigest() != manifest["validity_mask_sha256"]
                or len(data) != manifest["region"]["length"]
                or sum(mask) != manifest["stable_bytes"]
            ):
                raise ValueError("Capture integrity check failed")
            return cls(manifest["region"]["start"], data, mask)
        except (KeyError, TypeError) as exc:
            raise ValueError("Invalid capture manifest") from exc
