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
