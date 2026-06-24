"""Per-username config (nickname, persona, shame threshold) and persistent
per-user streak tracking.

crew.json shape (all fields optional except the key):

{
  "dave": {
    "nickname": "Inting Dave",
    "persona": "the guy who feeds every game and blames his jungler",
    "min_shame": 5
  },
  "steve": {
    "nickname": "Vision Andy",
    "persona": "a support main who somehow has worse vision than the ADC"
  }
}

Keys are lowercased riot game names. Lookups are case-insensitive.
"""
from __future__ import annotations

import json
import pathlib

_DIR = pathlib.Path(__file__).parent
CREW_FILE = _DIR / "crew.json"
STREAK_FILE = _DIR / "streaks.json"


def load_crew() -> dict[str, dict]:
    if CREW_FILE.exists():
        raw = json.loads(CREW_FILE.read_text())
        return {k.lower(): v for k, v in raw.items()}
    return {}


def profile_for(crew: dict[str, dict], name: str) -> dict:
    """Return this user's profile, or an empty dict if not configured."""
    return crew.get(name.lower(), {})


def lol_name_for_discord_id(crew: dict[str, dict], discord_id: str | int) -> str | None:
    """Reverse-lookup: Discord ID → LoL game name (the crew.json key)."""
    sid = str(discord_id)
    for lol_name, profile in crew.items():
        if str(profile.get("discord_id", "")) == sid:
            return lol_name
    return None


# --- streaks: roastable games in a row per user ---

def _load_streaks() -> dict[str, dict]:
    if STREAK_FILE.exists():
        return json.loads(STREAK_FILE.read_text())
    return {}


def _save_streaks(d: dict[str, dict]) -> None:
    STREAK_FILE.write_text(json.dumps(d, indent=2))


def peek_streak(name: str) -> dict:
    """Read-only projected streak (streak + 1) for feeding into prompts before committing."""
    streaks = _load_streaks()
    rec = streaks.get(name.lower(), {"streak": 0, "worst_streak": 0, "total_roasts": 0})
    projected = dict(rec)
    projected["streak"] = rec["streak"] + 1
    projected["worst_streak"] = max(rec["worst_streak"], projected["streak"])
    return projected


def update_streak(name: str, roastable: bool) -> dict:
    """Record whether this user's latest game was roastable and return their
    running totals: {streak, worst_streak, total_roasts}.
    """
    streaks = _load_streaks()
    key = name.lower()
    rec = streaks.get(key, {"streak": 0, "worst_streak": 0, "total_roasts": 0})
    if roastable:
        rec["streak"] += 1
        rec["total_roasts"] += 1
        rec["worst_streak"] = max(rec["worst_streak"], rec["streak"])
    else:
        rec["streak"] = 0
    streaks[key] = rec
    _save_streaks(streaks)
    return rec
