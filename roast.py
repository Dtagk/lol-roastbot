"""Turn a participant stat line into a 'shame score' and an Ollama roast."""
from __future__ import annotations

import aiohttp


def _normalize_position(p: dict) -> str:
    """Common position vocabulary across Match-V5 and LCU.

    Only UTILITY (support) is special-cased downstream. Match-V5 gives a
    clean teamPosition; LCU exposes lane/role which we map best-effort.
    """
    pos = (p.get("teamPosition") or "").upper()
    if pos:
        return pos
    role = (p.get("individualPosition") or "").upper()
    if "SUPPORT" in role:
        return "UTILITY"
    return role or "UNKNOWN"


def summarize(p: dict, game_duration_s: int) -> dict:
    """Extract the stats that make for good roasting."""
    mins = max(game_duration_s / 60, 1)
    k, d, a = p["kills"], p["deaths"], p["assists"]
    kda = (k + a) / max(d, 1)
    return {
        "champion": p["championName"],
        "win": p["win"],
        "kills": k, "deaths": d, "assists": a,
        "kda": round(kda, 2),
        "damage": p["totalDamageDealtToChampions"],
        "damage_taken": p.get("totalDamageTaken", 0),
        "position": _normalize_position(p),
        "duration_min": round(mins, 1),
    }


def shame_score(s: dict) -> int:
    score = 0
    if s["kda"] < 1.0:
        score += 20
    if s["kda"] < 0.5:
        score += 10
    dmg_per_min = s["damage"] / s["duration_min"]
    if dmg_per_min < 400:
        score += 10
    if dmg_per_min < 200:
        score += 10
    ratio = s["damage_taken"] / max(s["damage"], 1)
    if ratio > 2:
        score += 10
    if ratio > 4:
        score += 10
    return score


def _prompt(name: str, s: dict, profile: dict | None = None,
            streak: dict | None = None) -> str:
    profile = profile or {}
    result = "won" if s["win"] else "lost"
    display = profile.get("nickname") or name

    persona_line = ""
    if profile.get("persona"):
        persona_line = (
            f"Context on this player: they are known as {profile['persona']}. "
            f"Lean into that reputation.\n"
        )
    streak_line = ""
    if streak and streak.get("streak", 0) > 1:
        streak_line = (
            f"This is their {streak['streak']} roastable game in a row "
            f"(worst ever: {streak['worst_streak']}). Mock the streak.\n"
        )

    return (
        f"You are a witty Discord roast bot for a League of Legends friend group. "
        f"Write ONE short, savage-but-friendly roast (max 2 sentences, no preamble) "
        f"about {display}'s last game. Be funny, not genuinely mean. "
        f"Reference the actual stats.\n\n"
        f"{persona_line}{streak_line}"
        f"Player: {display}\n"
        f"Champion: {s['champion']} ({s['position']})\n"
        f"Result: {result} in {s['duration_min']} min\n"
        f"KDA: {s['kills']}/{s['deaths']}/{s['assists']} ({s['kda']})\n"
        f"Damage dealt: {s['damage']}\n"
        f"Damage taken: {s['damage_taken']}\n\n"
        f"Roast:"
    )


async def roast(name: str, s: dict, ollama_url: str, model: str,
                profile: dict | None = None, streak: dict | None = None) -> str:
    payload = {"model": model,
               "prompt": _prompt(name, s, profile, streak),
               "stream": False,
               "options": {"temperature": 0.9}}
    async with aiohttp.ClientSession() as sess:
        async with sess.post(f"{ollama_url}/api/generate", json=payload) as r:
            r.raise_for_status()
            data = await r.json()
    return data["response"].strip()
