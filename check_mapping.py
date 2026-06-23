"""One-shot: fetch last 10 games, print all teammate names, flag crew matches/misses."""
import asyncio, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from lcu import LCUClient, LCUError
from crew import load_crew

CREW_CFG = load_crew()

async def main():
    try:
        async with LCUClient() as lcu:
            stubs = await lcu.recent_games(0, 20)
            games = [await lcu.game(g["gameId"]) for g in stubs]
    except LCUError as e:
        print(f"LCU error: {e}"); return

    seen = {}
    for g in games:
        parts = LCUClient.participants(g)
        for p in parts:
            name = p["riotIdGameName"] or p["summonerName"]
            if not name or name == "?":
                continue
            seen[name] = CREW_CFG.get(name.lower())

    mapped, unmapped = [], []
    for name, profile in sorted(seen.items()):
        if profile:
            mapped.append((name, profile.get("nickname", ""), profile.get("discord_id", "")))
        else:
            unmapped.append(name)

    print(f"\n{'='*55}")
    print(f"{'LOL NAME':<25} {'NICKNAME':<15} DISCORD ID")
    print(f"{'='*55}")
    for name, nick, did in mapped:
        print(f"[OK] {name:<23} {nick:<15} {did}")
    print(f"\n--- Not in crew.json ({len(unmapped)}) ---")
    for name in unmapped:
        print(f"   {name}")
    print()

asyncio.run(main())
