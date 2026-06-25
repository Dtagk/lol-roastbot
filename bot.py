"""Discord roast bot. Polls the local League Client (LCU) for new matches
and roasts premade teammates via Ollama. No Riot dev key required.
"""
from __future__ import annotations

import asyncio
import os
import pathlib

from dotenv import load_dotenv
load_dotenv(encoding="utf-8-sig", override=True)

import discord
from discord.ext import tasks

from lcu import LCUClient, LCUError, LCUMountError, load_champion_map
import random
from roast import summarize, shame_score, roast, glaze, chat
from crew import load_crew, profile_for, update_streak, peek_streak, lol_name_for_discord_id
from history import record_game, get_summary
import jsonstore
import queue_store  # NEW: persistent dual-mode retry queue
import roast_memory  # NEW: remembers recent roast lines to avoid repetition

# --- config via env ---
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))
MIN_SHAME = int(os.environ.get("MIN_SHAME", "10"))
LCU_LOCKFILE = os.environ.get("LCU_LOCKFILE")  # optional override

# Optional allowlist: only roast these names (your premade crew). Empty = whole team.
CREW = {c.strip().lower() for c in os.environ.get("CREW", "").split(",") if c.strip()}

# Per-username profiles (nickname / persona / min_shame) from crew.json.
CREW_CFG = load_crew()

STATE = pathlib.Path(__file__).parent / "seen.json"


def load_seen() -> dict:
    return jsonstore.load(STATE, {})


def save_seen(d: dict) -> None:
    jsonstore.save(STATE, d)


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

seen = load_seen()
self_puuid: str = ""
last_seen: int | None = seen.get("last_game_id")
# Tracks whether we've already alerted on a broken mount, so the poll loop
# logs it once rather than every tick. Reset to False on a successful poll.
_mount_alerted: bool = False


async def send_or_queue(channel, content: str, *, kind: str = "roast") -> None:
    """Send a message; on failure queue it for re-send instead of dropping it.
    Used for every roast and clapback so both paths are covered."""
    try:
        await channel.send(content)
    except Exception as e:
        print(f"send failed ({kind}): {e}")
        queue_store.enqueue_send(channel.id, content, kind=kind, reason=str(e))


async def drain_queue(lcu) -> None:
    """Retry queued work at the top of each poll tick. `lcu` is an open LCUClient
    used to re-fetch games for regen entries."""
    for it in queue_store.pending():
        channel = client.get_channel(it["channel_id"])
        if channel is None:
            continue
        try:
            if it["type"] == "send":
                await channel.send(it["content"])
            elif it["type"] == "regen":
                full = await lcu.game(it["game_id"])
                await roast_game(full, channel)  # may raise -> mark_attempt below
            queue_store.resolve(it["id"])
        except Exception as e:
            queue_store.mark_attempt(it["id"], str(e))
    for it in queue_store.dead():
        label = it.get("kind") or it.get("type")
        print(f"giving up on queued {label} after "
              f"{it['attempts']} attempts: {it.get('last_error')}")


async def _warmup_ollama() -> bool:
    import aiohttp
    try:
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(f"{OLLAMA_URL}/api/generate", json={
                "model": OLLAMA_MODEL, "prompt": "hi", "stream": False,
                "think": False, "options": {"num_predict": 10},
            }) as r:
                await r.json()
        print(f"Ollama model {OLLAMA_MODEL} warmed up")
        return True
    except Exception as e:
        print(f"Ollama warmup failed: {e}")
        return False


@client.event
async def on_ready():
    global self_puuid, last_seen
    print(f"Logged in as {client.user}")
    await load_champion_map()
    warmed = await _warmup_ollama()
    channel = client.get_channel(CHANNEL_ID)
    if warmed and channel:
        await channel.send("🤖 online and ready to roast.")
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
    global last_seen, _mount_alerted
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        return
    try:
        async with LCUClient(LCU_LOCKFILE) as lcu:
            _mount_alerted = False  # mount is healthy again; re-arm the alert
            # populate self_puuid lazily if startup LCU was unavailable
            global self_puuid
            if not self_puuid:
                self_puuid = await lcu.current_puuid()
                print(f"poll: resolved self_puuid={self_puuid[:8]}...")

            # 1) retry anything queued from a previous tick (sends + regens)
            await drain_queue(lcu)

            # 2) handle new games
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
                    # Generation/fetch failed: queue the GAME ID so the next tick
                    # re-fetches and regenerates. Advancing last_seen below is now
                    # safe because the game isn't lost — it lives in the queue.
                    print(f"roast failed {g.get('gameId')}: {e}")
                    queue_store.enqueue_regen(g["gameId"], channel.id, str(e))
    except LCUMountError as e:
        # Broken volume / relocated install — needs a human, so surface it
        # loudly but only once per occurrence to avoid log spam every tick.
        if not _mount_alerted:
            print(f"[ALERT] LCU mount problem: {e}")
            _mount_alerted = True
        return
    except LCUError as e:
        print(f"lcu poll: {e}")
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

    # 4+ crew: always glaze MVP + roast the 3 worst so nobody's left out.
    # ≤3 crew: 10% random glaze instead of roasting (existing behaviour).
    always_glaze = len(candidates) >= 4
    print(f"candidates={len(candidates)} always_glaze={always_glaze}")
    if not always_glaze and random.random() < 0.1:
        mvp_name, mvp_s, mvp_profile = mvp
        line = await glaze(mvp_name, mvp_s, OLLAMA_URL, OLLAMA_MODEL, mvp_profile)
        display = mvp_profile.get("nickname") or mvp_name
        mention = f"<@{mvp_profile['discord_id']}> " if mvp_profile.get("discord_id") else ""
        await send_or_queue(channel, f"{'🏆' if won else '💀'} **{result}** ({mins} min)\n```\n{table}\n```")
        await send_or_queue(channel, f"✨ {mention}**{display}** carried so hard even I have to admit it.\n{line}")
        return

    # Generate all roast lines BEFORE sending or committing state, so an Ollama
    # failure aborts cleanly (re-raises -> regen queue) without half-posting.
    rendered = []
    for name, s, profile in targets:
        line = await roast(name, s, OLLAMA_URL, OLLAMA_MODEL, profile,
                           peek_streak(name), get_summary(name),
                           avoid=roast_memory.recent_roasts(name))
        display = profile.get("nickname") or name
        mention = f"<@{profile['discord_id']}> " if profile.get("discord_id") else ""
        rendered.append((name, s, profile, line,
                         f"🔥 {mention}**{display}** — {s['champion']} "
                         f"{s['kills']}/{s['deaths']}/{s['assists']}\n{line}"))

    # All roast generation succeeded -> commit state and send.
    target_names = {t[0] for t in targets}
    for name, s, profile in candidates:
        if name not in target_names:
            update_streak(name, False)

    await send_or_queue(channel, f"{'🏆' if won else '💀'} **{result}** ({mins} min)\n```\n{table}\n```")
    for name, s, profile, line, content in rendered:
        record_game(name, s)
        update_streak(name, True)
        roast_memory.record_roast(name, line)
        await send_or_queue(channel, content)

    # Glaze is a bonus — generate and send after roasts so a timeout/error
    # here skips the glaze without losing the roasts.
    if always_glaze:
        try:
            mvp_name, mvp_s, mvp_profile = mvp
            glaze_line = await glaze(mvp_name, mvp_s, OLLAMA_URL, OLLAMA_MODEL, mvp_profile)
            mvp_display = mvp_profile.get("nickname") or mvp_name
            mvp_mention = f"<@{mvp_profile['discord_id']}> " if mvp_profile.get("discord_id") else ""
            await send_or_queue(channel, f"✨ {mvp_mention}**{mvp_display}** carried so hard even I have to admit it.\n{glaze_line}")
        except Exception as e:
            print(f"glaze failed for {mvp[0]}: {e}")


async def _recent_context(channel, exclude_id: int, limit: int = 6) -> str:
    """Last few channel messages as plain text for short-term clapback context.
    Excludes the triggering message (added separately as the latest) AND the
    bot's own messages. The latter matters: the bot's prior roasts contain other
    crew members' personal facts (from their profiles), and feeding those back in
    as 'context' makes the model recycle one person's personal jabs onto a
    different target. Only human chatter is legitimate clapback context."""
    msgs = [m async for m in channel.history(limit=limit * 3 + 1)]
    msgs.reverse()  # oldest first
    lines = [
        f"{m.author.display_name}: {m.clean_content}"
        for m in msgs
        if m.clean_content
        and m.id != exclude_id
        and m.author.id != client.user.id  # never feed our own roasts back as ammo
    ]
    return "\n".join(lines[-limit:])


MAX_TAG_TARGETS = int(os.environ.get("MAX_TAG_TARGETS", "3"))


async def _handle_target(message, lcu_games, *, name, display,
                         mention, profile, reason, sender=None):
    """Generate + send one clapback/roast for a single resolved target.
    `name` is the lookup key (lowercased lol name, or the display name for a
    non-crew user). Raises on generation failure so the caller can fall back."""
    p = None
    duration = 0
    for g in lcu_games:
        parts = LCUClient.participants(g)
        found = next(
            (x for x in parts
             if (x["riotIdGameName"] or x["summonerName"]).lower() == name.lower()),
            None,
        )
        if found:
            p = found
            duration = g.get("gameDuration", 0)
            break

    if p:
        # target played a recent game -> stats-based roast
        s = summarize(p, duration)
        line = await roast(name, s, OLLAMA_URL, OLLAMA_MODEL, profile,
                           peek_streak(name), get_summary(name),
                           avoid=roast_memory.recent_roasts(name))
        record_game(name, s)
        update_streak(name, True)
        roast_memory.record_roast(name, line)
        content = (f"🔥 {mention} **{display}** — {s['champion']} "
                   f"{s['kills']}/{s['deaths']}/{s['assists']}\n{line}")
        await send_or_queue(message.channel, content, kind="roast")
    else:
        # not in the latest game -> cocky general chat with channel context
        convo = await _recent_context(message.channel, message.id)
        line = await chat(
            display, OLLAMA_URL, OLLAMA_MODEL, profile,
            reason or "they tagged you with nothing to say",
            get_summary(name), convo,
            avoid=roast_memory.recent_roasts(name),
            sender=sender,
        )
        roast_memory.record_roast(name, line)
        await send_or_queue(message.channel, f"{mention} {line}", kind="clapback")


@client.event
async def on_message(message):
    if message.author == client.user:
        return

    print(f"on_message id={message.id} author={message.author} content={message.content[:80]!r}")
    # Support both direct user mention and role mention (e.g. @Var_Bot as a role)
    bot_mentioned = client.user in message.mentions
    if not bot_mentioned and message.role_mentions and message.guild:
        member = message.guild.get_member(client.user.id)
        if member:
            bot_mentioned = any(r in member.roles for r in message.role_mentions)
    if not bot_mentioned:
        return

    # "fetch" command: immediately roast the latest game without waiting for poll
    import re as _re
    clean = _re.sub(r"<@[!&]?\d+>", "", message.content).strip()
    if clean.lower() == "reload":
        global CREW_CFG
        CREW_CFG = load_crew()
        await message.channel.send("crew reloaded.")
        return
    if clean.lower() == "fetch":
        channel = client.get_channel(CHANNEL_ID)
        full = None
        last_err = None
        print(f"fetch triggered by {message.author}")
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(3)
            try:
                async with LCUClient(LCU_LOCKFILE) as lcu:
                    global self_puuid
                    if not self_puuid:
                        self_puuid = await lcu.current_puuid()
                        print(f"fetch: got self_puuid={self_puuid[:8]}...")
                    games = await lcu.recent_games(0, 1)
                    if not games:
                        await channel.send("no games found.")
                        return
                    full = await lcu.game(games[0]["gameId"])
                print(f"fetch: got game {games[0]['gameId']} on attempt {attempt}")
                break
            except LCUError as e:
                print(f"fetch attempt {attempt}: {e}")
                last_err = e
        if full is None:
            e = last_err
            msg = "League client isn't running — open it first." if "lockfile not found" in str(e) else f"LCU not ready yet: {e}"
            await channel.send(msg)
            return
        try:
            await roast_game(full, channel)
        except Exception as e:
            print(f"fetch roast failed: {e}")
            await channel.send("roast generation blew up, check logs.")
        return

    # Decide intent. A message that mentions other users can mean two very
    # different things:
    #   1. an imperative aimed at them — "roast @X", "say congrats to @X"
    #      -> those users are the roast targets
    #   2. a question or remark aimed at the BOT that merely references them —
    #      "@Var_Bot is @X good mentally?", "do you think @X or @Y?"
    #      -> the SENDER is the target; the others are just context
    # Without this split, any mention becomes a roast victim (e.g. someone asks
    # the bot a question and an innocently-referenced friend gets roasted).
    other_mentions = [u for u in message.mentions if u != client.user]

    # Build the target list. Each target is a (name, display, discord_id,
    # profile) tuple. Crew members resolve to their crew.json key + profile;
    # everyone else falls back to their Discord display name with an empty
    # profile (plain roast, no persona ammo).
    targets = []
    seen_ids = set()
    if other_mentions:
        for u in other_mentions:
            if u.id in seen_ids:
                continue
            seen_ids.add(u.id)
            lol_name = lol_name_for_discord_id(CREW_CFG, u.id)
            if lol_name:
                prof = profile_for(CREW_CFG, lol_name)
                disp = prof.get("nickname") or lol_name
                targets.append((lol_name, disp, u.id, prof))
            else:
                # not in crew.json -> plain roast by display name
                targets.append((u.display_name, u.display_name, u.id, {}))
            if len(targets) >= MAX_TAG_TARGETS:
                break

    # Only bot tagged (no other mentions) -> reply to the SENDER.
    if not targets:
        lol_name = lol_name_for_discord_id(CREW_CFG, message.author.id)
        if lol_name:
            prof = profile_for(CREW_CFG, lol_name)
            disp = prof.get("nickname") or lol_name
            targets.append((lol_name, disp, message.author.id, prof))
        else:
            targets.append((message.author.display_name,
                            message.author.display_name,
                            message.author.id, {}))

    # Extract the reason/question text. Keep referenced people's names readable
    # so the model has context ("...or BossDyr?" must not become "...or ?"),
    # but drop the bot's own mention since it's just the trigger.
    reason = message.clean_content
    bot_handles = [f"@{client.user.display_name}"]
    member = message.guild.get_member(client.user.id) if message.guild else None
    if member and member.display_name:
        bot_handles.append(f"@{member.display_name}")
    for handle in bot_handles:
        reason = reason.replace(handle, "").strip()
    reason = reason.strip()

    # fetch the last 5 games (full data via game()) for target lookup
    try:
        async with LCUClient(LCU_LOCKFILE) as lcu:
            summaries = await lcu.recent_games(0, 5)
            lcu_games = [await lcu.game(s["gameId"]) for s in summaries]
    except LCUError:
        lcu_games = []

    # roast each target independently: one failure doesn't sink the others.
    for name, display, discord_id, profile in targets:
        mention = f"<@{discord_id}>"
        try:
            await _handle_target(message, lcu_games, name=name,
                                  display=display, mention=mention,
                                  profile=profile, reason=reason,
                                  sender=message.author.display_name)
        except Exception as e:
            print(f"clapback generation failed for {name}: {e}")
            await send_or_queue(
                message.channel,
                f"{mention} my brain lagged harder than your last teamfight. Tag me again.",
                kind="clapback",
            )


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
