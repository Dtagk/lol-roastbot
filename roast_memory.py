"""Per-player memory of recent roast lines, so the bot stops repeating itself.

Small models (llama3.2:3b) reach for the same insult given the same stats. We keep
the last N roast lines per player and feed them into the prompt as a "don't reuse
these" block. The model still sees the stats and persona, but is steered away from
phrasings it just used.

Stored in roast_lines.json (bind-mount it like the other state files). Keyed by
lowercased player name; each value is a capped list of recent lines, newest last.
"""

from __future__ import annotations

import pathlib

import jsonstore

_FILE = pathlib.Path(__file__).parent / "roast_lines.json"
KEEP = 20  # how many recent lines to remember per player


def recent_roasts(name: str) -> list[str]:
    """Return this player's recent roast lines (newest last), or []."""
    data = jsonstore.load(_FILE, {})
    return data.get(name.lower(), [])


def record_roast(name: str, line: str) -> None:
    """Append a roast line to a player's memory, capped at KEEP."""
    if not line:
        return
    data = jsonstore.load(_FILE, {})
    key = name.lower()
    lines = data.get(key, [])
    lines.append(line.strip())
    data[key] = lines[-KEEP:]
    jsonstore.save(_FILE, data)
