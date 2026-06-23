"""Discord roast bot. Polls the local League Client (LCU) for new matches
and roasts premade teammates via Ollama. No Riot dev key required.
"""
from __future__ import annotations

import json
import os
import pathlib

import discord
from discord.ext import tasks

from lcu import LCUClient, LCUError, load_champion_map
from roast import summarize, shame_score, roast
from crew import load_crew, profile_for, update_streak

# --- config via env ---
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))
MIN_SHAME = int(os.environ.get("MIN_SHAME", "10"))
LCU_LOCKFILE = os.environ.get("LCU_LOCKFILE")  # optional override

# Optional allowlist: only roast these names (your premade crew). Empty = whole team.
CREW = {c.strip().lower() for c in os.environ.get("CREW", "").split(",") if c.strip()}

# Per-username profiles (nickname / persona / min_shame) from crew.json.
CREW_CFG = load_crew()

STATE = pathlib.Path(__file__).parent / "seen.json"


def load_seen() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_seen(d: dict) -> None:
    STATE.write_text(json.dumps(d, indent=2))


intents = discord.Intents.default()
client = discord.Client(intents=intents)

seen = load_seen()
self_puuid: str = ""
last_seen: int | None = seen.get("last_game_id")


@client.event
async def on_ready():
    global self_puuid, last_seen
    print(f"Logged in as {client.user}")
    await load_champion_map()
    try:
        async with LCUClient(LCU_LOCKFILE) as lcu:
            self_puuid = await lcu.current_puuid()
            if last_seen is None:
                games = await lcu.recent_games(0, 1)
                if games:
                    last_seen = games[0]["gameId"]
                    seen["last_game_id"] = last_seen
    except LCUError as e:
        print(f"LCU not ready: {e}")
    save_seen(seen)
    poll.start()


@tasks.loop(seconds=POLL_SECONDS)
async def poll():
    global last_seen
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        return
    try:
        async with LCUClient(LCU_LOCKFILE) as lcu:
            games = await lcu.recent_games(0, 5)
            new = []
            for g in games:  # newest first
                if g["gameId"] == last_seen:
                    break
                new.append(g)
            for g in reversed(new):  # oldest-new first
                try:
                    full = await lcu.game(g["gameId"])
                    await roast_game(full, channel)
                except Exception as e:
                    print(f"roast failed {g.get('gameId')}: {e}")
    except LCUError as e:
        print(f"lcu poll failed (client closed?): {e}")
        return
    if games:
        last_seen = games[0]["gameId"]
        seen["last_game_id"] = last_seen
        save_seen(seen)


async def roast_game(game: dict, channel) -> None:
    parts = LCUClient.participants(game)
    me = next((p for p in parts if p["puuid"] == self_puuid), None)
    if me is None:
        return
    my_team = me["teamId"]
    duration = game.get("gameDuration", 0)

    teammates = [p for p in parts if p["teamId"] == my_team]
    won = me["win"]
    header = "🏆 GG" if won else "💀 L"
    posted = False
    for p in teammates:
        name = p["riotIdGameName"] or p["summonerName"]
        if CREW and name.lower() not in CREW:
            continue
        s = summarize(p, duration)
        profile = profile_for(CREW_CFG, name)
        threshold = profile.get("min_shame", MIN_SHAME)
        roastable = shame_score(s) >= threshold
        streak = update_streak(name, roastable)
        if not roastable:
            continue
        line = await roast(name, s, OLLAMA_URL, OLLAMA_MODEL, profile, streak)
        display = profile.get("nickname") or name
        mention = f"<@{profile['discord_id']}> " if profile.get("discord_id") else ""
        await channel.send(
            f"🔥 {mention}**{display}** — {s['champion']} "
            f"{s['kills']}/{s['deaths']}/{s['assists']}\n{line}"
        )
        posted = True
    if not posted and not won:
        await channel.send(f"{header} — somehow nobody played badly enough to roast. Suspicious.")


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
