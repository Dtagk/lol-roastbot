"""Persistent retry queue for work the bot couldn't complete on the first try.

Two entry kinds, because two different things can fail:

  * SEND retry  — a message was generated fine but failed to reach Discord
                  (network/503). Stores the rendered text; retried by re-sending.
                  Covers both game roasts and @mention clapbacks.

  * REGEN retry — a game's roast failed during *generation* (Ollama hiccup) so no
                  text exists yet. Stores the game id; retried by re-fetching the
                  game from the LCU and regenerating. Only the poll loop produces
                  these — clapbacks are never regen-queued, since the latest game
                  may roll over and a regenerated clapback would be stale.

Both kinds share an attempt counter and the MAX_ATTEMPTS cap. State lives in
queue.json (bind-mount it so it survives restarts).
"""

from __future__ import annotations

import pathlib
import time
import uuid

import jsonstore

QUEUE = pathlib.Path(__file__).parent / "queue.json"
MAX_ATTEMPTS = 5


def _load() -> list[dict]:
    return jsonstore.load(QUEUE, [])


def _save(items: list[dict]) -> None:
    jsonstore.save(QUEUE, items)


def enqueue_send(channel_id: int, content: str, *, kind: str = "roast",
                 reason: str = "") -> str:
    """Queue a generated message that failed to send. Returns the entry id."""
    items = _load()
    entry_id = uuid.uuid4().hex
    items.append({
        "id": entry_id,
        "type": "send",
        "channel_id": channel_id,
        "content": content,
        "kind": kind,
        "attempts": 1,
        "last_error": reason,
        "ts": time.time(),
    })
    _save(items)
    return entry_id


def enqueue_regen(game_id: int, channel_id: int, reason: str = "") -> str:
    """Queue a game whose roast failed to generate. De-dupes on game_id: if the
    game is already queued, bump its attempt count instead of adding a duplicate."""
    items = _load()
    for it in items:
        if it.get("type") == "regen" and it.get("game_id") == game_id:
            it["attempts"] = it.get("attempts", 0) + 1
            it["last_error"] = reason
            it["ts"] = time.time()
            _save(items)
            return it["id"]
    entry_id = uuid.uuid4().hex
    items.append({
        "id": entry_id,
        "type": "regen",
        "game_id": game_id,
        "channel_id": channel_id,
        "attempts": 1,
        "last_error": reason,
        "ts": time.time(),
    })
    _save(items)
    return entry_id


def pending() -> list[dict]:
    """Entries still worth retrying (under the attempt cap), oldest first."""
    return [it for it in _load() if it.get("attempts", 0) < MAX_ATTEMPTS]


def mark_attempt(entry_id: str, reason: str = "") -> None:
    """Bump the attempt counter after a retry fails."""
    items = _load()
    for it in items:
        if it["id"] == entry_id:
            it["attempts"] = it.get("attempts", 0) + 1
            it["last_error"] = reason
            it["ts"] = time.time()
            break
    _save(items)


def resolve(entry_id: str) -> None:
    """Remove an entry after it completes successfully."""
    _save([it for it in _load() if it["id"] != entry_id])


def dead() -> list[dict]:
    """Entries that hit the attempt cap — surfaced for logging, then dropped."""
    items = _load()
    out = [it for it in items if it.get("attempts", 0) >= MAX_ATTEMPTS]
    if out:
        _save([it for it in items if it.get("attempts", 0) < MAX_ATTEMPTS])
    return out
