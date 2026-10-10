"""Microphones available through PipeWire, for the Setup tab's picker."""

import json
import subprocess


def microphones():
    """[(node name, description)] of capture sources, without the monitors of outputs."""
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True, text=True, timeout=3)
        sources = json.loads(out.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    return [
        (s["name"], s.get("description") or s["name"])
        for s in sources
        if s.get("properties", {}).get("media.class") == "Audio/Source"
    ]
