"""Per-player game history: rolling stats, champion records, worst game ever."""
from __future__ import annotations

import json
import pathlib

_FILE = pathlib.Path(__file__).parent / "history.json"


def _load() -> dict:
    return json.loads(_FILE.read_text()) if _FILE.exists() else {}


def _save(d: dict) -> None:
    _FILE.write_text(json.dumps(d, indent=2))


def record_game(name: str, s: dict) -> None:
    data = _load()
    key = name.lower()
    rec = data.setdefault(key, {"recent": [], "champs": {}, "worst": None})

    rec["recent"].append({"champ": s["champion"], "win": s["win"],
                          "kda": s["kda"], "deaths": s["deaths"]})
    rec["recent"] = rec["recent"][-10:]

    c = rec["champs"].setdefault(s["champion"], {"games": 0, "wins": 0, "deaths": 0})
    c["games"] += 1
    if s["win"]:
        c["wins"] += 1
    c["deaths"] += s["deaths"]

    if rec["worst"] is None or s["deaths"] > rec["worst"]["deaths"]:
        rec["worst"] = {"champ": s["champion"], "deaths": s["deaths"], "kda": s["kda"]}

    _save(data)


def get_summary(name: str) -> dict | None:
    rec = _load().get(name.lower())
    if not rec or not rec["recent"]:
        return None

    recent = rec["recent"]
    avg_deaths = round(sum(r["deaths"] for r in recent) / len(recent), 1)
    avg_kda = round(sum(r["kda"] for r in recent) / len(recent), 2)

    champs = rec["champs"]
    fav = max(champs, key=lambda c: champs[c]["games"]) if champs else None

    return {
        "games_tracked": len(recent),
        "avg_deaths": avg_deaths,
        "avg_kda": avg_kda,
        "favorite_champ": fav,
        "fav_record": f"{champs[fav]['wins']}W-{champs[fav]['games']-champs[fav]['wins']}L" if fav else None,
        "fav_avg_deaths": round(champs[fav]["deaths"] / champs[fav]["games"], 1) if fav else None,
        "worst": rec.get("worst"),
    }
