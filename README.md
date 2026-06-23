# lol-roastbot

Discord bot that polls the League of Legends client after each game and posts Ollama-generated roasts of your premade crew. No Riot API key required — reads the local LCU lockfile directly.

## How it works

- Polls the LCU match history every 2 minutes (`POLL_SECONDS`)
- For each new game, posts a score table with KDA, a 👑 MVP crown, and a ⚓ anchor
- Roasts the worst player plus anyone over their own shame threshold, capped at 3
- ~10% of games it glazes the MVP instead of roasting anyone
- Also responds when @mentioned: stats-based roast if the target played the latest game, otherwise a persona-only roast driven by whatever reason you give

## Setup

### 1. Prerequisites

- League of Legends client installed and running
- [Ollama](https://ollama.com) running locally with a model pulled (default: `gpt-oss:20b`)
- Docker (for the bot container)
- A Discord bot token and channel ID

### 2. Configure

Copy `.env.example` to `.env` and fill in:

```
DISCORD_TOKEN=your_bot_token
DISCORD_CHANNEL_ID=your_channel_id
CREW=PlayerOne,PlayerTwo,...   # LoL game names, comma-separated
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=gpt-oss:20b
POLL_SECONDS=120
MIN_SHAME=10
```

Create `crew.json` (gitignored — contains private Discord IDs):

```json
{
  "PlayerOne": {
    "discord_id": "123456789",
    "nickname": "P1",
    "persona": "description used to personalise roasts",
    "min_shame": 10
  }
}
```

### 3. Run

```powershell
# Start Ollama container (from gym-knowledge-repository)
.\start_ollama.ps1

# Start the roastbot
docker compose up -d --build
```

### 4. Auto-start with League (optional)

Register `watch_league.ps1` as a Task Scheduler task (run as Administrator):

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PWD\watch_league.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "RoastBot - Watch League" -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force
```

## Force-post a game

```powershell
Get-Content .env | ForEach-Object { if ($_ -match "^([^#=][^=]*)=(.*)$") { [System.Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim()) } }
.venv\Scripts\python.exe post_latest.py
```

## Shame score

Roasts are gated by a per-player `min_shame` threshold (default `MIN_SHAME`). Score is based on:

| Factor | Points |
|--------|--------|
| Each death | +3 |
| KDA < 1.0 | +10 |
| Damage/min < 400 | +10 |
| Damage taken > 2× dealt | +10 |

The single worst player is always roasted even if below threshold; everyone else needs to clear their own `min_shame`. Max 3 roasts per game.
