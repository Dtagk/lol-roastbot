"""Turn a participant stat line into a 'shame score' and an Ollama roast."""

from __future__ import annotations

import aiohttp
import re

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

_CHAMP_BURNS = {
    "Yasuo": "the champion of players who blame lag and wind",
    "Yone": "Yasuo's brother, equally chosen by people who should be in therapy",
    "Master Yi": "the ctrl+Z of League — brainless split-push and pray",
    "Twitch": "a rat who thinks he's a hypercarry but just dies in the sewer before 6",
    "Vayne": "the champion picked by people who want to feel skilled but int in the early game",
    "Zed": "the flashy assassin of players who watched one montage and thought they're Faker",
    "Teemo": "the most hated rat in the game, chosen by people with no friends",
    "Tryndamere": "the undying split-pusher picked when you've already given up on teamwork",
    "Akali": "the champion that looks broken in the hands of pros and trolls in yours",
    "Riven": "the mechanical goddess of players who can't clear a wave without burning flash",
    "Jinx": "the hypercarry of players who go 0/5 in lane then blame the support",
    "Heimerdinger": "the most passive-aggressive pick in the game",
    "Nasus": "the stacking simulator for players too scared to fight",
    "Katarina": "the pentakill or 0/10 champion, no in-between",
    "Rengar": "one-shot or be one-shot, the champion for people who hate nuance",
    "Draven": "the ego champion — the only one with a passive that mocks you for dying",
    "Irelia": "when someone wants to look skilled so they pick the 500-ability champion",
    "Lee Sin": "the champion that separates scripters from people who watched YouTube",
    "Singed": "running away from the fight and calling it 'macro play'",
    "Kalista": "the champion that requires an ADC main, a support main, and a therapist",
}


def _clean(text: str) -> str:
    """Strip any leaked reasoning block and surrounding whitespace."""
    return _THINK_RE.sub("", text).strip()


async def _generate(ollama_url: str, model: str, prompt: str,
                    num_predict: int = 6000, temperature: float = 0.9) -> str:
    """Single call path for every generation. Guards against the silent
    failure mode: gpt-oss burns its budget reasoning, _clean strips it, and
    response comes back empty. Explicit timeout so a hang surfaces instead of
    blocking the poll loop forever."""
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": num_predict},
        "think": False,
    }
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.post(f"{ollama_url}/api/generate", json=payload) as r:
            r.raise_for_status()
            data = await r.json()
    return _clean(data.get("response", "")) or "...I got nothing. That one speaks for itself."


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
        "kills": k,
        "deaths": d,
        "assists": a,
        "kda": round(kda, 2),
        "damage": p["totalDamageDealtToChampions"],
        "damage_taken": p.get("totalDamageTaken", 0),
        "position": _normalize_position(p),
        "duration_min": round(mins, 1),
    }


def shame_score(s: dict) -> int:
    score = 0
    score += s["deaths"] * 3
    if s["kda"] < 1.0:
        score += 10
    dmg_per_min = s["damage"] / s["duration_min"]
    if dmg_per_min < 400:
        score += 10
    ratio = s["damage_taken"] / max(s["damage"], 1)
    if ratio > 2:
        score += 10
    return score


def _prompt(
    name: str, s: dict, profile: dict | None = None, streak: dict | None = None,
    history: dict | None = None,
) -> str:
    profile = profile or {}
    result = "won" if s["win"] else "lost"
    display = profile.get("nickname") or name

    persona_line = ""
    if profile.get("persona"):
        persona_line = (
            f"Context on this player: they are known as {profile['persona']}. "
            f"Lean into that reputation.\n"
        )
    personal_line = ""
    if profile.get("personal"):
        personal_line = f"Personal facts: {'; '.join(profile['personal'])}.\n"
    streak_line = ""
    if streak and streak.get("streak", 0) > 1:
        streak_line = (
            f"This is their {streak['streak']} roastable game in a row "
            f"(worst ever: {streak['worst_streak']}). Mock the streak.\n"
        )
    history_line = ""
    if history:
        parts = [f"last {history['games_tracked']} games avg: {history['avg_deaths']} deaths, KDA {history['avg_kda']}"]
        if history.get("favorite_champ"):
            parts.append(
                f"most played: {history['favorite_champ']} ({history['fav_record']}, "
                f"{history['fav_avg_deaths']} deaths/game)"
            )
        if history.get("worst"):
            w = history["worst"]
            parts.append(f"worst game ever: {w['deaths']} deaths on {w['champ']}")
        history_line = "History: " + "; ".join(parts) + ".\n"
    champ_burn = _CHAMP_BURNS.get(s["champion"], "")
    is_fav = s["champion"] in (profile.get("favorites") or [])
    fav_note = f" This is one of their declared favorite champions — no excuses." if is_fav else ""
    champ_line = f"Champion note: {s['champion']} is known as {champ_burn}.{fav_note} Lean into it.\n" if (champ_burn or is_fav) else ""
    if not champ_burn and is_fav:
        champ_line = f"Champion note: {s['champion']} is one of their declared mains — no excuses for this performance.\n"

    return (
        f"You are a blunt, sarcastic Discord bot roasting your friend group after their League games. "
        f"You talk like a close friend who doesn't censor himself — casual, sharp, occasionally drops a swear if it fits, but never tries too hard. "
        f"Write ONE roast (max 2 sentences, no preamble, no hashtags). Reference the actual stats.\n\n"
        f"{persona_line}{personal_line}{streak_line}{history_line}{champ_line}"
        f"Player: {display}\n"
        f"Champion: {s['champion']} ({s['position']})\n"
        f"Result: {result} in {s['duration_min']} min\n"
        f"KDA: {s['kills']}/{s['deaths']}/{s['assists']} ({s['kda']})\n"
        f"Damage dealt: {s['damage']}\n"
        f"Damage taken: {s['damage_taken']}\n\n"
        f"Roast:"
    )


def _persona_prompt(name: str, profile: dict, reason: str = "",
                    history: dict | None = None) -> str:
    display = profile.get("nickname") or name
    persona_line = ""
    if profile.get("persona"):
        persona_line = f"Context: they are known as {profile['persona']}.\n"
    personal_line = ""
    if profile.get("personal"):
        personal_line = f"Personal facts: {'; '.join(profile['personal'])}.\n"
    reason_line = f"Reason for roast: {reason}\n" if reason else ""
    history_line = ""
    if history:
        parts = [f"last {history['games_tracked']} games avg: {history['avg_deaths']} deaths, KDA {history['avg_kda']}"]
        if history.get("favorite_champ"):
            parts.append(
                f"most played: {history['favorite_champ']} ({history['fav_record']}, "
                f"{history['fav_avg_deaths']} deaths/game)"
            )
        if history.get("worst"):
            w = history["worst"]
            parts.append(f"worst game ever: {w['deaths']} deaths on {w['champ']}")
        history_line = "History: " + "; ".join(parts) + ".\n"
    return (
        f"You are a blunt, sarcastic Discord bot roasting your friend group after their League games. "
        f"You talk like a close friend who doesn't censor himself — casual, sharp, occasionally drops a swear if it fits, but never tries too hard. "
        f"Write ONE roast about {display} (max 2 sentences, no preamble, no hashtags).\n\n"
        f"{persona_line}{personal_line}{reason_line}{history_line}"
        f"Roast:"
    )


async def roast_persona(
    name: str, ollama_url: str, model: str, profile: dict, reason: str = "",
    history: dict | None = None,
) -> str:
    return await _generate(
        ollama_url, model, _persona_prompt(name, profile, reason, history)
    )


def _glaze_prompt(name: str, s: dict, profile: dict | None = None) -> str:
    profile = profile or {}
    display = profile.get("nickname") or name
    result = "won" if s["win"] else "lost"
    persona_line = f"Context: {profile['persona']}.\n" if profile.get("persona") else ""
    return (
        f"You are an over-the-top hype bot for a League of Legends friend group. "
        f"Write ONE short, absurdly glowing tribute (max 2 sentences, no preamble) "
        f"about {display}'s last game. Go full sycophant — they are a god among players.\n\n"
        f"{persona_line}"
        f"Player: {display}\n"
        f"Champion: {s['champion']} ({s['position']})\n"
        f"Result: {result} in {s['duration_min']} min\n"
        f"KDA: {s['kills']}/{s['deaths']}/{s['assists']} ({s['kda']})\n"
        f"Damage dealt: {s['damage']}\n\n"
        f"Glaze:"
    )


async def glaze(
    name: str, s: dict, ollama_url: str, model: str, profile: dict | None = None
) -> str:
    return await _generate(ollama_url, model, _glaze_prompt(name, s, profile))


async def roast(
    name: str,
    s: dict,
    ollama_url: str,
    model: str,
    profile: dict | None = None,
    streak: dict | None = None,
    history: dict | None = None,
) -> str:
    return await _generate(
        ollama_url, model, _prompt(name, s, profile, streak, history)
    )


def _chat_prompt(display: str, profile: dict | None, reason: str,
                 history: dict | None = None, convo: str = "") -> str:
    """Cocky general-chat reply when the target isn't in the latest game."""
    profile = profile or {}
    persona_line = (
        f"You're talking to {display}, known as {profile['persona']}.\n"
        if profile.get("persona") else ""
    )
    history_line = ""
    if history:
        history_line = (
            f"For ammo if relevant: last {history['games_tracked']} games avg "
            f"{history['avg_deaths']} deaths, KDA {history['avg_kda']}.\n"
        )
    convo_line = f"Recent conversation:\n{convo}\n\n" if convo else ""
    return (
        f"You are a blunt, sarcastic Discord bot in a League of Legends friend group. "
        f"You talk like a close friend who doesn't censor himself — casual, sharp, occasionally drops a swear if it fits. "
        f"Reply to the latest message in 1-2 sentences. Clap back directly to what was said. No preamble, no hashtags.\n\n"
        f"{persona_line}{history_line}{convo_line}"
        f"Latest message: {reason}\n\n"
        f"Reply:"
    )


async def chat(
    display: str, ollama_url: str, model: str, profile: dict | None = None,
    reason: str = "", history: dict | None = None, convo: str = "",
) -> str:
    return await _generate(
        ollama_url, model, _chat_prompt(display, profile, reason, history, convo)
    )
