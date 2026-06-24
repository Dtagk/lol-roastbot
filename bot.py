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
import random

from roast import summarize, shame_score, roast, roast_persona, glaze, chat
from crew import load_crew, profile_for, update_streak, lol_name_for_discord_id
from history import record_game, get_summary

# --- config via env ---
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gpt-oss:20b")
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
intents.message_content = True
client = discord.Client(intents=intents)

seen = load_seen()
self_puuid: str = ""
last_seen: int | None = seen.get("last_game_id")


async def _warmup_ollama():
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(f"{OLLAMA_URL}/api/generate", json={
                "model": OLLAMA_MODEL, "prompt": "hi", "stream": False,
                "options": {"num_predict": 1}
            })
        print(f"Ollama model {OLLAMA_MODEL} warmed up")
    except Exception as e:
        print(f"Ollama warmup failed: {e}")


@client.event
async def on_ready():
    global self_puuid, last_seen
    print(f"Logged in as {client.user}")
    await load_champion_map()
    await _warmup_ollama()
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

    # ~10% chance: glaze the MVP instead of roasting everyone
    if random.random() < 0.1:
        mvp_name, mvp_s, mvp_profile = mvp
        line = await glaze(mvp_name, mvp_s, OLLAMA_URL, OLLAMA_MODEL, mvp_profile)
        display = mvp_profile.get("nickname") or mvp_name
        mention = f"<@{mvp_profile['discord_id']}> " if mvp_profile.get("discord_id") else ""
        await channel.send(f"✨ {mention}**{display}** carried so hard even I have to admit it.\n{line}")
        return

    # pick who to roast: the single worst, plus anyone over their own
    # min_shame threshold, capped at 3 total (descending shame).
    candidates = crew_stats + enemy_stats
    ranked = sorted(candidates, key=lambda x: shame_score(x[1]), reverse=True)

    targets = []
    for name, s, profile in ranked:
        threshold = profile.get("min_shame", MIN_SHAME)
        is_worst = not targets  # first in ranked order is the worst
        if is_worst or shame_score(s) >= threshold:
            targets.append((name, s, profile))
        if len(targets) == 3:
            break

    # streaks: roastable = made the target list
    target_names = {t[0] for t in targets}
    for name, s, profile in candidates:
        if name not in target_names:
            update_streak(name, False)

    for name, s, profile in targets:
        record_game(name, s)
        streak = update_streak(name, True)
        line = await roast(name, s, OLLAMA_URL, OLLAMA_MODEL, profile, streak, get_summary(name))
        display = profile.get("nickname") or name
        mention = f"<@{profile['discord_id']}> " if profile.get("discord_id") else ""
        await channel.send(f"🔥 {mention}**{display}** — {s['champion']} {s['kills']}/{s['deaths']}/{s['assists']}\n{line}")


async def _recent_context(channel, exclude_id: int, limit: int = 6) -> str:
    """Last few channel messages as plain text for short-term clapback context.
    Excludes the triggering message (added separately as the latest)."""
    msgs = [m async for m in channel.history(limit=limit + 1)]
    msgs.reverse()  # oldest first
    lines = [
        f"{m.author.display_name}: {m.clean_content}"
        for m in msgs
        if m.clean_content and m.id != exclude_id
    ]
    return "\n".join(lines[-limit:])


@client.event
async def on_message(message):
    if message.author == client.user:
        return
    if client.user not in message.mentions:
        return

    # find the target: first mentioned crew member that isn't the bot, else the sender
    target_lol = None
    for u in message.mentions:
        if u == client.user:
            continue
        name = lol_name_for_discord_id(CREW_CFG, u.id)
        if name:
            target_lol = name
            target_discord_id = u.id
            break
    if not target_lol:
        target_lol = lol_name_for_discord_id(CREW_CFG, message.author.id)
        target_discord_id = message.author.id

    if not target_lol:
        await message.channel.send("I don't know who you are. Get good first.")
        return

    profile = profile_for(CREW_CFG, target_lol)
    display = profile.get("nickname") or target_lol
    mention = f"<@{target_discord_id}>"

    # extract reason from message (everything after the mentions)
    reason = message.clean_content
    for word in [f"@{u.display_name}" for u in message.mentions]:
        reason = reason.replace(word, "").strip()
    reason = reason.strip()

    # Look for the target in the latest game. If they aren't in it, roast
    # purely from their persona + whatever reason was given in the message.
    try:
        async with LCUClient(LCU_LOCKFILE) as lcu:
            games = await lcu.recent_games(0, 1)
            game = await lcu.game(games[0]["gameId"]) if games else None
    except LCUError:
        game = None

    p = None
    if game:
        parts = LCUClient.participants(game)
        duration = game.get("gameDuration", 0)
        p = next(
            (x for x in parts
             if (x["riotIdGameName"] or x["summonerName"]).lower() == target_lol),
            None,
        )

    if p:
        # target played the latest game -> stats-based roast
        s = summarize(p, duration)
        record_game(target_lol, s)
        streak = update_streak(target_lol, True)
        line = await roast(target_lol, s, OLLAMA_URL, OLLAMA_MODEL, profile, streak, get_summary(target_lol))
        await message.channel.send(
            f"🔥 {mention} **{display}** — {s['champion']} "
            f"{s['kills']}/{s['deaths']}/{s['assists']}\n{line}"
        )
    else:
        # not in the latest game -> cocky general chat with channel context
        convo = await _recent_context(message.channel, message.id)
        line = await chat(
            display, OLLAMA_URL, OLLAMA_MODEL, profile,
            reason or "they tagged you with nothing to say",
            get_summary(target_lol), convo,
        )
        await message.channel.send(f"{mention} {line}")


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
