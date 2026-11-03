"""Declarative physical layout and firmware-derived electrical mappings."""

import json
from importlib.resources import files


def load_layout():
    return json.loads(files("razer_emu_gui").joinpath("assets/blackwidow-v4-75.json").read_text())


def key_colors(layout, snapshots):
    """Convert immutable driver snapshots to key colours, respecting current and enables."""
    levels = [snapshot.levels for snapshot in snapshots]
    return {
        key["id"]: "#"
        + "".join(f"{round(levels[key['controller']][c] * 255):02x}" for c in key["rgb"])
        for key in (*layout["keys"], *layout["underglow"])
    }
