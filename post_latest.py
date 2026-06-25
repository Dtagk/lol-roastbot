"""One-shot: fetch latest game from LCU and post roast to Discord."""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(__file__))

import discord
from lcu import LCUClient, LCUError, load_champion_map
from roast import summarize, shame_score, roast
from crew import load_crew, profile_for, update_streak

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")
MIN_SHAME = int(os.environ.get("MIN_SHAME", "10"))
CREW = {c.strip().lower() for c in os.environ.get("CREW", "").split(",") if c.strip()}
CREW_CFG = load_crew()

intents = discord.Intents.default()
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    channel = client.get_channel(CHANNEL_ID)

    await load_champion_map()

    async with LCUClient() as lcu:
        self_puuid = await lcu.current_puuid()
        games = await lcu.recent_games(0, 1)
        if not games:
            print("No games found")
            await client.close()
            return
        game_id = games[0]["gameId"]
        print(f"Fetching game {game_id}")
        game = await lcu.game(game_id)

    parts = LCUClient.participants(game)
    me = next((p for p in parts if p["puuid"] == self_puuid), None)
    if me is None:
        print("Could not find self in game")
        await client.close()
        return

    my_team = me["teamId"]
    duration = game.get("gameDuration", 0)
    teammates = [p for p in parts if p["teamId"] == my_team]
    won = me["win"]
    result = "GG" if won else "L"
    mins = round(duration / 60)

    crew_stats = []
    for p in teammates:
        name = p["riotIdGameName"] or p["summonerName"]
        if CREW and name.lower() not in CREW:
            continue
        s = summarize(p, duration)
        profile = profile_for(CREW_CFG, name)
        crew_stats.append((name, s, profile))

    if not crew_stats:
        print("No crew members found in game")
        await client.close()
        return

    mvp = max(crew_stats, key=lambda x: x[1]["kda"])
    lines = []
    for name, s, profile in crew_stats:
        display = profile.get("nickname") or name
        crown = " 👑" if name == mvp[0] else ""
        lines.append(f"{display:<12} {s['champion']:<12} {s['kills']}/{s['deaths']}/{s['assists']}{crown}")
    table = "\n".join(lines)
    await channel.send(f"{'🏆' if won else '💀'} **{result}** ({mins} min)\n```\n{table}\n```")
    print("Score table posted")

    posted = False
    for name, s, profile in crew_stats:
        threshold = profile.get("min_shame", MIN_SHAME)
        roastable = shame_score(s) >= threshold
        streak = update_streak(name, roastable)
        if not roastable:
            print(f"  {name}: shame {shame_score(s)} < {threshold}, skipping")
            continue
        print(f"  Roasting {name} (shame={shame_score(s)})...")
        line = await roast(name, s, OLLAMA_URL, OLLAMA_MODEL, profile, streak)
        display = profile.get("nickname") or name
        mention = f"<@{profile['discord_id']}> " if profile.get("discord_id") else ""
        await channel.send(f"🔥 {mention}**{display}** — {s['champion']} {s['kills']}/{s['deaths']}/{s['assists']}\n{line}")
        posted = True

    if not posted and not won:
        await channel.send("Somehow nobody played badly enough to roast. Suspicious.")

    print("Done")
    await client.close()

client.run(DISCORD_TOKEN)
