"""Fetch and cache the official top-view photograph separately from package code."""

import hashlib
from pathlib import Path
from urllib.request import urlopen

PHOTO_URL = (
    "https://apps.razer.com/synapse/products/677/ui/677_0/PluginImages/677_0_1_dashboard3x.png"
)
PHOTO_SHA256 = "dac11c5e9df3c26530f6a98bac2c25ac51c94d3b4daee198f5498a3fd50906e8"


def keyboard_photo(cache_directory):
    path = Path(cache_directory) / "blackwidow-v4-75-ansi.png"
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == PHOTO_SHA256:
        return path
    with urlopen(PHOTO_URL, timeout=20) as response:
        data = response.read(4_000_001)
    if len(data) > 4_000_000 or hashlib.sha256(data).hexdigest() != PHOTO_SHA256:
        raise ValueError(
            "Keyboard photograph changed or is incomplete; use --background with a local photo"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
    return path
