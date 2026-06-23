"""LCU (League Client) data source. No Riot dev key, no daily refresh.

Reads the local client's lockfile for port + password and queries the
unofficial LCU match-history endpoints. The client must be running and
logged in on the same machine as the bot.
"""
from __future__ import annotations

import base64
import os
import pathlib
import ssl

import aiohttp


class LCUError(Exception):
    pass


def _default_lockfile() -> pathlib.Path:
    # Windows default install; override with LCU_LOCKFILE env if elsewhere.
    env = os.environ.get("LCU_LOCKFILE")
    if env:
        return pathlib.Path(env)
    return pathlib.Path(
        os.environ.get("ProgramFiles", r"C:\Riot Games"),
    ).parent / "Riot Games" / "League of Legends" / "lockfile"


def read_lockfile(path: pathlib.Path | None = None) -> tuple[int, str]:
    """Return (port, password) from the client lockfile.

    Format: ProcessName:PID:port:password:protocol
    """
    path = path or _default_lockfile()
    if not path.exists():
        raise LCUError(
            f"lockfile not found at {path}. Is the League client running? "
            f"Set LCU_LOCKFILE to its path if installed elsewhere."
        )
    parts = path.read_text().strip().split(":")
    if len(parts) < 5:
        raise LCUError(f"unexpected lockfile format: {parts}")
    return int(parts[2]), parts[3]


class LCUClient:
    def __init__(self, lockfile: str | None = None):
        self._lockfile = pathlib.Path(lockfile) if lockfile else None
        self._session: aiohttp.ClientSession | None = None
        self._base = ""

    async def __aenter__(self):
        port, password = read_lockfile(self._lockfile)
        token = base64.b64encode(f"riot:{password}".encode()).decode()
        # LCU uses a self-signed cert -> disable verification.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Basic {token}"},
            connector=aiohttp.TCPConnector(ssl=ctx),
        )
        lcu_host = os.environ.get("LCU_HOST", "127.0.0.1")
        self._base = f"https://{lcu_host}:{port}"
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    async def _get(self, path: str) -> dict | list:
        assert self._session
        async with self._session.get(self._base + path) as r:
            if r.status >= 400:
                raise LCUError(f"{r.status} for {path}: {await r.text()}")
            return await r.json()

    async def current_puuid(self) -> str:
        data = await self._get("/lol-summoner/v1/current-summoner")
        return data["puuid"]

    async def recent_games(self, begin: int = 0, end: int = 5) -> list[dict]:
        """Recent games for the logged-in summoner, newest first.

        Returns the raw `games` list, each entry already containing full
        participant stats for the whole lobby.
        """
        path = (
            "/lol-match-history/v1/products/lol/current-summoner/matches"
            f"?begIndex={begin}&endIndex={end}"
        )
        data = await self._get(path)
        return data.get("games", {}).get("games", [])

    async def game(self, game_id: int) -> dict:
        return await self._get(f"/lol-match-history/v1/games/{game_id}")

    # --- shape helpers: LCU game json -> the dicts roast.py expects ---

    @staticmethod
    def participants(game: dict) -> list[dict]:
        """Flatten LCU participants + identities into the same field names
        roast.py/summarize already consume (championName, kills, win, etc.).
        """
        idents = {
            pi["participantId"]: pi.get("player", {})
            for pi in game.get("participantIdentities", [])
        }
        out = []
        for p in game.get("participants", []):
            st = p.get("stats", {})
            tl = p.get("timeline", {})
            player = idents.get(p["participantId"], {})
            name = player.get("gameName") or player.get("summonerName") or "?"
            out.append({
                "puuid": player.get("puuid", ""),
                "riotIdGameName": player.get("gameName", ""),
                "summonerName": player.get("summonerName", ""),
                "teamId": p.get("teamId"),
                "championName": _CHAMP.get(p.get("championId"), str(p.get("championId"))),
                "win": st.get("win", False),
                "kills": st.get("kills", 0),
                "deaths": st.get("deaths", 0),
                "assists": st.get("assists", 0),
                "totalMinionsKilled": st.get("totalMinionsKilled", 0),
                "neutralMinionsKilled": st.get("neutralMinionsKilled", 0),
                "totalDamageDealtToChampions": st.get("totalDamageDealtToChampions", 0),
                "visionScore": st.get("visionScore", 0),
                "goldEarned": st.get("goldEarned", 0),
                "teamPosition": tl.get("lane", ""),
                "individualPosition": tl.get("role", ""),
            })
        return out


# Minimal championId->name map is filled lazily from Data Dragon at startup.
_CHAMP: dict[int, str] = {}


async def load_champion_map() -> None:
    """Populate championId -> name from Data Dragon (no key needed)."""
    global _CHAMP
    if _CHAMP:
        return
    ctx = ssl.create_default_context()
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx)) as s:
        async with s.get("https://ddragon.leagueoflegends.com/api/versions.json") as r:
            ver = (await r.json())[0]
        url = f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/en_US/champion.json"
        async with s.get(url) as r:
            data = await r.json()
    _CHAMP = {int(v["key"]): v["id"] for v in data["data"].values()}
