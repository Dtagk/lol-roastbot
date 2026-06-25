# CLAUDE.md — LoL Premade Roast Bot setup runbook

You are an agent setting up a Discord bot that polls one player's match
history via the Riot API and posts Ollama-generated roasts of the premade
team after each new game. Work through phases in order. Stop at every
`🛑 CHECKPOINT` for user approval. Never perform `⚠️ USER ACTION` steps
yourself — they involve account creation, credentials, or UI a human must do.

## Files in this folder
- `bot.py` — Discord client, poll loop, per-match team roasting
- `riot.py` — async Riot API client (Account-V1 + Match-V5, regional routing, 429 retry)
- `roast.py` — stat summarizer, `shame_score` heuristic, Ollama `/api/generate` call
- `requirements.txt` — discord.py, aiohttp
- `.env.example` — config template

## How it works (so you can explain/debug)
- Track only the user's own riot ID (`SELF_ID`). Premade group = one match
  contains every teammate's stat line in `info.participants`.
- Each poll pulls the last 5 match ids, processes any newer than `last_seen`,
  and persists progress to `seen.json` (oldest-new processed first).
- For each new match: find the user's `teamId`, take all participants on that
  team, optionally filter to `CREW`, and roast anyone with `shame_score >= MIN_SHAME`.

---

## Phase 0 — Preflight
- Confirm Python 3.10+ (`python --version`; the code uses `str | None` unions).
- Confirm Ollama is installed and running locally, with a chat model pulled
  (default `llama3.1:8b`). Verify: `curl http://localhost:11434/api/tags`.
- Confirm this is the target machine (the user's Windows rig per their setup,
  or wherever Ollama lives). Ollama must be reachable at `OLLAMA_URL`.

🛑 CHECKPOINT: Report Python version, Ollama status, and available models.
Wait for the user to confirm before proceeding.

---

## Phase 1 — Discord application
⚠️ USER ACTION — do NOT do this yourself; guide the user:
1. Go to https://discord.com/developers/applications → New Application.
2. Bot tab → reset/copy the **bot token** (this is `DISCORD_TOKEN`).
3. No privileged intents are required (the bot only sends messages).
4. OAuth2 → URL Generator → scopes `bot`, permission `Send Messages`.
   Open the generated URL and invite the bot to the server.
5. In Discord, enable Developer Mode, right-click the target channel →
   Copy Channel ID (this is `DISCORD_CHANNEL_ID`).

🛑 CHECKPOINT: Confirm the bot appears in the server's member list and the
user has the token + channel id ready. Do not ask the user to paste the token
into chat — it goes in `.env` only.

---

## Phase 2 — Riot API key
⚠️ USER ACTION:
1. Sign in at https://developer.riotgames.com.
2. Copy the **Development API Key** (`RGAPI-...`) → this is `RIOT_API_KEY`.
   Note: dev keys expire every 24h. For a persistent bot the user must
   register a Personal or Production key (separate application + approval).
3. Identify the user's riot ID as `GameName#TAG` and their platform
   (`euw1`, `na1`, `kr`, etc.) → `SELF_ID`, `RIOT_PLATFORM`.

🛑 CHECKPOINT: Confirm key type (dev vs personal/production) and warn the user
if it's a dev key that the bot will stop working after 24h.

---

## Phase 3 — Configure
1. Copy `.env.example` to `.env`.
2. Fill in: `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID`, `RIOT_API_KEY`,
   `RIOT_PLATFORM`, `SELF_ID`.
3. Set `CREW` to the premade members' game names (comma-separated, no tags),
   e.g. `Me,Dave,Steve`. Leave blank to roast the whole team.
4. Tune `MIN_SHAME` (default 10 — raises the bar; ~50+ = only disasters) and
   `POLL_SECONDS` (default 120). Set `OLLAMA_MODEL` to a pulled model.
5. Ensure `.env` is never committed: add it to `.gitignore`.

🛑 CHECKPOINT: Show the user the filled `.env` with the token and API key
MASKED (show only last 4 chars). Confirm values before running.

---

## Phase 4 — Install
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |  Unix: source .venv/bin/activate
pip install -r requirements.txt
```

---

## Phase 5 — Dry run / validation (no Discord posting)
Before going live, validate Riot + Ollama wiring with a throwaway script:
```python
import asyncio, os
from riot import RiotClient
from roast import summarize, shame_score, roast

async def main():
    name, tag = os.environ["SELF_ID"].split("#")
    async with RiotClient(os.environ["RIOT_API_KEY"], os.environ["RIOT_PLATFORM"]) as r:
        puuid = await r.get_puuid(name, tag)
        ids = await r.recent_match_ids(puuid, 1)
        m = await r.match(ids[0])
        me = r.participant(m, puuid)
        s = summarize(me, m["info"]["gameDuration"])
        print(s, "shame=", shame_score(s))
        print(await roast(name, s, os.environ.get("OLLAMA_URL","http://localhost:11434"),
                          os.environ.get("OLLAMA_MODEL","qwen2.5:14b")))

asyncio.run(main())
```
Expected: a stat dict, a shame score, and one roast line. If Riot returns 403,
the key is expired/invalid. If Ollama errors, check the model name and that
the server is up.

🛑 CHECKPOINT: Show the user the sample roast. Confirm tone/quality is right
before going live. If too tame or too harsh, adjust the prompt in
`roast.py::_prompt` (temperature, the savage-but-friendly instruction).

---

## Phase 6 — Run
```bash
python bot.py
```
On first boot it seeds `last_seen` to the most recent match, so it will NOT
roast a backlog — only games played after launch. To keep it running, use a
process manager (Windows: Task Scheduler / NSSM; Unix: systemd / pm2).

🛑 CHECKPOINT: Confirm "Logged in as ..." prints and `seen.json` is created.
Have the user play (or wait for) one game and verify a roast posts.

---

## Troubleshooting
- **No posts after a game**: shame score below `MIN_SHAME` (lower it), or the
  player name isn't in `CREW`, or the bot lacks Send Messages in that channel.
- **403 from Riot**: dev key expired (regenerate) or wrong platform routing.
- **429 spam**: dev keys are rate-limited; `riot.py` already retries on
  `Retry-After`, but reduce polling frequency if it persists.
- **Ollama timeout / connection refused**: model not pulled, or `OLLAMA_URL`
  wrong (remote host needs `OLLAMA_HOST=0.0.0.0` on the server).
- **Empty / weird roasts**: try a larger model or lower temperature in `_prompt`.

## Guardrails
- Never paste the Discord token or Riot key into chat or commit them.
- Keep it friendly — this roasts friends, not strangers; respect the `CREW`
  allowlist so random fill teammates aren't targeted.
