# Switching the roast model

The bot talks to a local Ollama instance. Which model it uses is controlled by
one setting — `OLLAMA_MODEL` in your `.env` — plus actually having that model
pulled into the Ollama container. This guide walks through both.

Recommended model: **`qwen2.5:14b`** (Q4_K_M, ~9 GB). Fits a 16 GB card with
room for context, no reasoning-token overhead, and noticeably better at holding
"one sentence, no placeholders" than the 7B.

---

## TL;DR

```powershell
# 1. Pull the model into the bot's Ollama container
docker exec ollama-roastbot ollama pull qwen2.5:14b

# 2. Point the bot at it
#    edit .env  ->  OLLAMA_MODEL=qwen2.5:14b

# 3. Restart just the bot (Ollama can keep running)
docker compose up -d --build roastbot
```

That's it. The next game (or a forced `post_latest.py`) uses the new model.

---

## Step by step

### 1. Pull the model

Ollama runs in its own container (`ollama-roastbot`) with a persistent volume,
so models survive restarts. Pull into that container, **not** a host-level
Ollama:

```powershell
docker exec ollama-roastbot ollama pull qwen2.5:14b
```

Confirm it landed:

```powershell
docker exec ollama-roastbot ollama list
```

You should see `qwen2.5:14b` in the list.

### 2. Update `.env`

Change the one line:

```
OLLAMA_MODEL=qwen2.5:14b
```

> Note: `OLLAMA_MODEL` in `.env` is the single source of truth. The fallback
> defaults baked into `bot.py`, `post_latest.py`, and `.env.example` are all
> aligned to `qwen2.5:14b`, so they only matter if you unset `OLLAMA_MODEL`
> entirely — and even then they agree.

### 3. Restart the bot

```powershell
docker compose up -d --build roastbot
```

On startup the bot warms the model with a tiny `"hi"` generation and logs
`Ollama model qwen2.5:14b warmed up`. If you see that line, you're good.
First call after a fresh pull is slow (model loads into VRAM); after that it's
warm.

---

## Verifying it works

Force a post against your most recent game without waiting for the poller:

```powershell
Get-Content .env | ForEach-Object { if ($_ -match "^([^#=][^=]*)=(.*)$") { [System.Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim()) } }
.venv\Scripts\python.exe post_latest.py
```

Watch the roast that comes out: one sentence, in English, no `??` placeholders.
If it rambles or drifts language, the model didn't switch — recheck `.env` and
that the restart actually rebuilt (`docker compose ps` should show a recent
"Up" time on `roastbot`).

---

## Trying other models

Any Ollama model works the same way — pull it, set `OLLAMA_MODEL`, restart.

| Model | Pull | VRAM (Q4) | Notes |
|-------|------|-----------|-------|
| `qwen2.5:14b` | `ollama pull qwen2.5:14b` | ~9 GB | **Recommended.** Best balance for this card. |
| `qwen2.5:7b` | `ollama pull qwen2.5:7b` | ~5 GB | Faster, weaker instruction-following. |
| `gemma3:12b` | `ollama pull gemma3:12b` | ~8 GB | Strong multilingual discipline (less language drift). Can pull punches on swearing. |
| `gpt-oss:20b` | `ollama pull gpt-oss:20b` | ~16 GB | Reasoning model — eats `num_predict` on thinking, hard to tune length. Avoid for one-liners. |

Remember to `docker exec ollama-roastbot ollama pull <model>` (into the
container), not a host Ollama.

### A note on length tuning

`num_predict` lives in `roast.py` (`_generate`, default 160). It's the **output
token cap**. Non-reasoning models (Qwen, Gemma) spend all of it on the roast, so
a lower value tightens output predictably — 80 is plenty for one sentence.
Reasoning models (gpt-oss) spend part of it *thinking*, which is exactly why
length was inconsistent before. If you stick with Qwen/Gemma you can safely
lower `num_predict` to tighten the roasts.

---

## Defaults

All fallback defaults are aligned to `qwen2.5:14b`:

- `bot.py` (`OLLAMA_MODEL` fallback)
- `post_latest.py` (`OLLAMA_MODEL` fallback)
- `.env.example`
- `CLAUDE.md` (example snippet)

`.env` overrides all of them. They exist only as a sane fallback if
`OLLAMA_MODEL` is ever unset.

---

## Freeing disk space

Pulled models pile up in the container volume. List and remove unused ones:

```powershell
docker exec ollama-roastbot ollama list
docker exec ollama-roastbot ollama rm gpt-oss:20b
```
