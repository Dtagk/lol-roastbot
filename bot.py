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
from crew import load_crew, profile_for, update_streak, lol_name_for_discord_id

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
    result = "GG" if won else "L"
    mins = round(duration / 60)

    # collect crew members who played
    crew_stats = []
    for p in teammates:
        name = p["riotIdGameName"] or p["summonerName"]
        if CREW and name.lower() not in CREW:
            continue
        s = summarize(p, duration)
        profile = profile_for(CREW_CFG, name)
        crew_stats.append((name, s, profile))

    if not crew_stats:
        return

    # crew members on the enemy team (custom games)
    enemy_stats = []
    for p in parts:
        if p["teamId"] == my_team:
            continue
        name = p["riotIdGameName"] or p["summonerName"]
        if CREW and name.lower() not in CREW:
            continue
        s = summarize(p, duration)
        profile = profile_for(CREW_CFG, name)
        enemy_stats.append((name, s, profile))

    # score table
    mvp = max(crew_stats, key=lambda x: x[1]["kda"])
    anchor = max(crew_stats, key=lambda x: shame_score(x[1]))
    lines = []
    for name, s, profile in crew_stats:
        display = profile.get("nickname") or name
        tag = " 👑" if name == mvp[0] else (" ⚓" if name == anchor[0] else "")
        lines.append(f"{display:<12} {s['champion']:<12} {s['kills']}/{s['deaths']}/{s['assists']}{tag}")
    if enemy_stats:
        lines.append("— vs —")
        for name, s, profile in enemy_stats:
            display = profile.get("nickname") or name
            lines.append(f"{display:<12} {s['champion']:<12} {s['kills']}/{s['deaths']}/{s['assists']}")
    table = "\n".join(lines)
    await channel.send(f"{'🏆' if won else '💀'} **{result}** ({mins} min)\n```\n{table}\n```")

    # roasts
    posted = False
    for name, s, profile in crew_stats + enemy_stats:
        threshold = profile.get("min_shame", MIN_SHAME)
        roastable = shame_score(s) >= threshold
        streak = update_streak(name, roastable)
        if not roastable:
            continue
        line = await roast(name, s, OLLAMA_URL, OLLAMA_MODEL, profile, streak)
        display = profile.get("nickname") or name
        mention = f"<@{profile['discord_id']}> " if profile.get("discord_id") else ""
        await channel.send(f"🔥 {mention}**{display}** — {s['champion']} {s['kills']}/{s['deaths']}/{s['assists']}\n{line}")
        posted = True
    if not posted and not won:
        await channel.send("Somehow nobody played badly enough to roast. Suspicious.")


@client.event
async def on_message(message):
    if message.author == client.user:
        return
    if client.user not in message.mentions:
        return

    lol_name = lol_name_for_discord_id(CREW_CFG, message.author.id)
    if not lol_name:
        await message.channel.send(f"I don't know who you are. Get good first.")
        return

    profile = profile_for(CREW_CFG, lol_name)
    display = profile.get("nickname") or lol_name
    await message.channel.send(f"On it, {display}...")

    try:
        async with LCUClient(LCU_LOCKFILE) as lcu:
            games = await lcu.recent_games(0, 1)
            if not games:
                await message.channel.send("No recent games found.")
                return
            game = await lcu.game(games[0]["gameId"])
    except LCUError as e:
        await message.channel.send(f"League client not reachable: {e}")
        return

    parts = LCUClient.participants(game)
    duration = game.get("gameDuration", 0)
    p = next(
        (x for x in parts if (x["riotIdGameName"] or x["summonerName"]).lower() == lol_name),
        None,
    )
    if p is None:
        await message.channel.send(f"Couldn't find {display} in their latest game.")
        return

    s = summarize(p, duration)
    streak = update_streak(lol_name, True)
    line = await roast(lol_name, s, OLLAMA_URL, OLLAMA_MODEL, profile, streak)
    mention = f"<@{message.author.id}>"
    await message.channel.send(f"🔥 {mention} **{display}** — {s['champion']} {s['kills']}/{s['deaths']}/{s['assists']}\n{line}")


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
