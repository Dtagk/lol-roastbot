# lol-roastbot

Discord bot that polls the League of Legends client after each game and posts Ollama-generated roasts of your premade crew. No Riot API key required — reads the local LCU lockfile directly.

## How it works

- Polls the LCU match history every 2 minutes (`POLL_SECONDS`)
- For each new game, posts a score table with KDA, a 👑 MVP crown, and a ⚓ anchor
- Roasts the worst player plus anyone over their own shame threshold, capped at 3
- ~10% of games it glazes the MVP instead of roasting anyone
- Also responds when @mentioned: stats-based roast if the target played the latest game, otherwise a persona-driven roast from whatever reason you give. Tag several people at once and it roasts each of them; tag someone who isn't in `crew.json` and it still roasts them by their Discord name (no persona, just a clean clapback)
- Any game whose roast fails to post (Ollama timeout, LCU drop, Discord error) is pushed onto a persistent retry queue and re-attempted on the next poll instead of being silently dropped

## Setup

### 1. Prerequisites

- League of Legends client installed and running
- Docker (for the bot container, and optionally Ollama — see below)
- A Discord bot token and channel ID
- An Ollama model pulled (default: `qwen2.5:7b`)

### 2. Configure

Copy `.env.example` to `.env` and fill in:

```
DISCORD_TOKEN=your_bot_token
DISCORD_CHANNEL_ID=your_channel_id
CREW=PlayerOne,PlayerTwo,...   # LoL game names, comma-separated
OLLAMA_URL=http://ollama:11434
OLLAMA_MODEL=qwen2.5:7b
POLL_SECONDS=120
MIN_SHAME=10
MAX_TAG_TARGETS=3   # max people roasted per @mention message
```

Create `crew.json` (gitignored — contains private Discord IDs):

```
{
  "PlayerOne": {
    "discord_id": "123456789",
    "nickname": "P1",
    "persona": "description used to personalise roasts",
    "min_shame": 10
  }
}
```

### 3. Model choice

Roasts are short and need wit more than reasoning. `qwen2.5:7b` is the default —
it's clever enough for good roasts, fits comfortably in 16 GB alongside the League
client, and responds effectively instantly on a 5060 Ti. On a 16 GB GPU:

| Model            | Size    | Notes                                            |
| ---------------- | ------- | ------------------------------------------------ |
| `qwen2.5:7b`     | ~4.7 GB | Default. Sharp roasts, plenty of headroom.       |
| `llama3.2:3b`    | ~2 GB   | Leaner/faster cold start; obvious-joke ceiling.  |
| `phi3.5`         | ~2.2 GB | Solid lightweight alternative.                   |
| `gpt-oss:20b`    | MoE     | Wittiest, but see the warning below.             |

**Warning on `num_predict`:** this patch caps generation at `num_predict=250` in
`roast.py`, which is correct for qwen2.5 / llama3.2 / phi3.5 (no reasoning block).
Do NOT use that cap with a reasoning model like `gpt-oss:20b` — it burns tokens on
a `<think>` block that gets stripped, so the budget truncates mid-reasoning and
returns an empty roast. If you run gpt-oss, raise `num_predict` back to ~6000.

Pull whichever you set in `.env`:

```
ollama pull qwen2.5:7b
```

### 4. Run

Ollama now runs as its own service in `docker-compose.yml` — the bot no longer
depends on the external `gym-knowledge-repository` / `start_ollama.ps1`:

```
docker compose up -d --build
docker compose exec ollama ollama pull qwen2.5:7b   # first run only
```

**Prefer Ollama running natively on Windows?** (GPU access is simpler that way.)
Comment out the `ollama` service block in `docker-compose.yml`, set
`OLLAMA_URL=http://host.docker.internal:11434` in `.env`, run `ollama serve`,
then `docker compose up -d --build` for just the bot.

### 5. Auto-start with League (optional)

Register `watch_league.ps1` as a Task Scheduler task (run as Administrator).

## Updating the crew without a rebuild

`crew.json` is bind-mounted into the container (read-only), so it is **not** baked
into the image. To change nicknames, personas, or thresholds:

1. Edit `crew.json` on the host.
2. `docker compose restart roastbot`

The bot reloads the file on restart — no `--build`, no image rebuild. (If `crew.py`
is wired to reload per-use, edits apply on the next game/mention with no restart at all.)

## Force-post a game

```
Get-Content .env | ForEach-Object { if ($_ -match "^([^#=][^=]*)=(.*)$") { [System.Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim()) } }
.venv\Scripts\python.exe post_latest.py
```

## Retry queue

The bot posts two kinds of message: **game roasts** (from the poll loop) and
**clapbacks** (from @mentions). The queue (`queue.json`, bind-mounted so it
survives restarts) handles two failure modes:

* **Send failure** (Discord/network, message already generated) → stores the
  rendered text and re-sends it next tick. Covers roasts *and* clapbacks.
* **Generation failure** in the poll loop (Ollama hiccup, no text yet) → stores
  the **game id** and re-fetches + regenerates the whole game next tick. This is
  why advancing `last_seen` past a failed game is safe: it isn't lost, it's queued.

Clapback generation failures are deliberately **not** regen-queued — the latest
game may roll over before the next tick, so a regenerated clapback would be stale.
Those just post a short "tag me again" reply. The poll loop drains the queue at the
top of every tick and gives up after `MAX_ATTEMPTS` (default 5, in `queue_store.py`),
logging the dead-lettered entry.

## Clapback isolation (personal facts don't bleed between people)

@mention clapbacks for someone not in the latest game use recent channel messages
as context. That context is filtered to **human chatter only** — the bot's own
prior roasts are excluded, because those carry other crew members' persona/personal
facts and the model would otherwise recycle one person's personal jabs onto a
different target. The `chat()` prompt also states that only the current target's own
profile facts are fair game and that names/details about other people in the
conversation are off-limits.

Persona and personal facts are framed as **optional flavour**, not mandatory: the
prompts tell the model to use them only when they make the roast funnier and that a
clean clapback at just the stats or the message is fine. This stops every roast
mechanically cramming in one persona detail plus one personal fact.

## Tagging: multiple targets and non-crew fallback

An @mention can name **more than one person**, and the bot roasts each tagged user
independently (deduped, capped at `MAX_TAG_TARGETS`, default 3 — override in `.env`).
Each target gets its own generation, so one failure posts a "tag me again" for that
person without sinking the others.

A tagged user who **isn't in `crew.json`** is still roasted — by their Discord
display name, with no profile. They have no persona or personal ammo, so the model
just claps back at the stats (if they're in the latest game) or the message itself.
Anti-repetition memory and streaks key off the resolved name, so non-crew targets
build their own history under their display name. Note: if such a user later changes
their Discord nickname, their history won't carry over (fine for ad-hoc targets).

If the bot is tagged with no other mention, it roasts the sender — crew member or
not. The old "I don't know who you are" reply is gone; an unknown sender now just
gets plain-roasted too.

## Not repeating itself

Repeats are attacked two ways. **Via the prompt:** the bot remembers the last few
roast lines per player (`roast_lines.json`, capped at 20, most recent 12 injected)
and feeds them in as a
"don't reuse these" block, so each new roast is told what it already said and
steered toward a different angle. **Via the sampler:** `_generate` sets Ollama
repetition penalties (`repeat_penalty` 1.3, `presence_penalty` 0.6,
`frequency_penalty` 0.3, `repeat_last_n` 256) and temperature 1.0, which discourage
recycled phrasing at the token level. The memory persists across restarts via the
bind mount; empty `roast_lines.json` to `{}` to reset it.

Note: the sampler penalties work on llama.cpp-backed models. Verify they take
effect on your chosen model — some newer models on Ollama's Go-native sampler can
silently ignore them. If you switch models and still see repeats, the prompt-side
memory is the reliable lever. Tune the penalty values in `_generate` (`roast.py`).

## State files

`seen.json`, `streaks.json`, `history.json`, `queue.json`, and `roast_lines.json`
are bind-mounted and **shipped as empty seeds** (`{}` / `[]`). Keep them in the repo
root: if the host path is missing when Docker mounts it, Docker creates a *directory*
there and the bot would crash trying to read it. `jsonstore.py` is a second line of
defence — it treats a missing, unreadable, or directory path as empty and writes
atomically.

## Shame score

Roasts are gated by a per-player `min_shame` threshold (default `MIN_SHAME`). Score is based on:

| Factor                  | Points |
| ----------------------- | ------ |
| Each death              | +3     |
| KDA < 1.0               | +10    |
| Damage/min < 400        | +10    |
| Damage taken > 2× dealt | +10    |

The single worst player is always roasted even if below threshold; everyone else needs to clear their own `min_shame`. Max 3 roasts per game.
