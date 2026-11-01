"""Reusable firmware emulator; importing the package does not initialize Renode."""

from .firmware import Firmware, FlashRegion

__all__ = ["Firmware", "FlashRegion"]
