"""Tiny JSON-file persistence helper shared by the state stores.

Centralises the load/save pattern that seen.json, streaks.json, history.json and
queue.json each used to reimplement. Crucially it guards the bind-mount trap:
when docker-compose mounts `./streaks.json` and the host path does not exist,
Docker creates it as a *directory*, after which `read_text()` raises IsADirectory
and the bot crash-loops. `load()` treats a missing OR unreadable path (including a
directory) as "empty", so a fresh checkout can't wedge the container.

Prefer shipping the empty seed files (see the repo root) so the mounts bind to
real files; this helper is the second line of defence.
"""

from __future__ import annotations

import json
import pathlib


def load(path: pathlib.Path, default):
    """Return parsed JSON at `path`, or a copy of `default` if missing/unreadable.

    `default` should be a fresh literal ({} or []) at each call site, not a shared
    mutable — this returns it as-is when the file is absent.
    """
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return default


def save(path: pathlib.Path, data) -> None:
    """Write `data` as indented JSON. Direct write — atomic rename breaks across
    bind-mount boundaries (container overlay FS → host FS = EBUSY/EXDEV)."""
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
