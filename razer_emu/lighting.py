"""Firmware-driven lighting snapshots, before the physical LED mapping is known."""

from dataclasses import dataclass


@dataclass(frozen=True)
class LightingState:
    """192 channels in driver register order, without a guessed RGB/key mapping."""

    pwm: bytes
    enabled: bytes
    global_current: int
    shutdown: bool
    unsupported_operation: str | None = None

    def to_dict(self):
        return {
            "pwm": self.pwm.hex(),
            "enabled_mask": self.enabled.hex(),
            "global_current": self.global_current,
            "shutdown": self.shutdown,
            "unsupported_operation": self.unsupported_operation,
        }

    @property
    def levels(self):
        """Normalized PWM × global-current levels; not calibrated light output."""
        if self.unsupported_operation:
            raise RuntimeError(self.unsupported_operation)
        return tuple(
            value * self.global_current / (255 * 255)
            if not self.shutdown and self.enabled[index // 8] & (1 << (index % 8))
            else 0.0
            for index, value in enumerate(self.pwm)
        )


class LightingController:
    """Read-only frontend view of a controller written by the original firmware."""

    def __init__(self, keyboard, backend, bus, address):
        self._keyboard = keyboard
        self._backend = backend
        self.bus = bus
        self.address = address

    def state(self):
        self._keyboard.state()  # owning Keyboard lifetime applies to this view
        return LightingState(
            bytes(self._backend.Pwm),
            bytes(self._backend.Enabled),
            int(self._backend.GlobalCurrent),
            bool(self._backend.Shutdown),
            str(self._backend.UnsupportedOperation) if self._backend.UnsupportedOperation else None,
        )
