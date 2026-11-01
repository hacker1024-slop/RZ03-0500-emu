"""Validated electrical input scripts, scheduled against the core's virtual clock."""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class _Event:
    at_us: int
    kind: str
    values: tuple


class InputScript:
    """An ordered JSON array of matrix, encoder and independent-button events."""

    def __init__(self, events):
        if not isinstance(events, list):
            raise ValueError("An input script must be a JSON array")
        parsed = []
        previous = 0
        fields = {
            "matrix": ("row", "column", "pressed"),
            "encoder": ("phase_a", "phase_b"),
            "button": ("index", "pressed"),
        }
        for index, item in enumerate(events):
            if not isinstance(item, dict):
                raise ValueError(f"Input event {index} must be an object")
            kind = item.get("type")
            if not isinstance(kind, str) or kind not in fields:
                raise ValueError(f"Input event {index} has an unknown type")
            if set(item) != {"at_us", "type", *fields[kind]}:
                raise ValueError(f"Input event {index} has missing or unknown fields")
            at = item["at_us"]
            if type(at) is not int or at < previous:
                raise ValueError(
                    "Event times must be nonnegative, nondecreasing integer microseconds"
                )
            for name in fields[kind]:
                value = item[name]
                if name in ("pressed", "phase_a", "phase_b"):
                    valid = type(value) is bool
                else:
                    limit = {"row": 8, "column": 16, "index": 2}[name]
                    valid = type(value) is int and 0 <= value < limit
                if not valid:
                    raise ValueError(f"Input event {index} has an invalid {name}")
            parsed.append(_Event(at, kind, tuple(item[name] for name in fields[kind])))
            previous = at
        self._events = tuple(parsed)

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def events(self):
        return self._events

    @property
    def duration_us(self):
        return self._events[-1].at_us if self._events else 0


class InputPlayback:
    """One playback prepared before execution; start after host setup if desired.

    Events at equal times retain file order. Finishing or cancelling does not
    release contacts implicitly. MCU reset cancels playback and preserves the
    existing contact-reset behavior. All access stays on the core's owning thread.
    """

    def __init__(self, keyboard, script):
        self._keyboard = keyboard
        self._script = script
        self._index = 0
        self._origin = None
        needed = {event.kind for event in script.events}
        self._targets = {}
        for kind, attribute in (
            ("matrix", "matrix"),
            ("encoder", "encoder"),
            ("button", "buttons"),
        ):
            if kind in needed:
                self._targets[kind] = (
                    getattr(keyboard, attribute) or getattr(keyboard, "attach_" + attribute)()
                )

    @property
    def script(self):
        return self._script

    @property
    def applied(self):
        return self._index

    @property
    def running(self):
        self._keyboard.state()
        return self._keyboard._input_playback is self

    @property
    def next_due_us(self):
        if self._origin is None or self._index == len(self.script.events):
            return None
        return self._origin + self.script.events[self._index].at_us

    def start(self):
        self._keyboard._ensure_running()
        if self._keyboard._input_playback is not None:
            raise RuntimeError("An input script is already playing")
        self._origin = self._keyboard.state().elapsed_us
        self._index = 0
        self._keyboard._input_playback = self
        self._apply_due(self._origin)

    def cancel(self):
        self._keyboard.state()
        if self._keyboard._input_playback is self:
            self._keyboard._input_playback = None

    def _apply_due(self, elapsed_us):
        while self.next_due_us is not None and self.next_due_us <= elapsed_us:
            event = self.script.events[self._index]
            target = self._targets[event.kind]
            if event.kind == "matrix":
                target.set_key(*event.values)
            elif event.kind == "encoder":
                target.set_phases(*event.values)
            else:
                target.set_pressed(*event.values)
            self._index += 1
        if self.next_due_us is None:
            self._keyboard._input_playback = None
