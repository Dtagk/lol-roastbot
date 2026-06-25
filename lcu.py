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
import time

import aiohttp


class LCUError(Exception):
    pass


class LCUMountError(LCUError):
    """The mount root itself is missing/empty — a config or relocation problem
    that needs a human, distinct from the client merely being closed."""
    pass


def _mount_root() -> pathlib.Path:
    """The directory mounted into the container that *contains* the League
    install. Defaults to the standard Riot parent dir. Mounting the parent
    (not the leaf game folder) survives patches that relocate the install."""
    return pathlib.Path(os.environ.get("LCU_MOUNT_ROOT", r"C:\Riot Games"))


def _is_league_lockfile(path: pathlib.Path) -> bool:
    """A lockfile is `ProcessName:PID:port:password:protocol`. Validate the
    process name so we never point at a stray/non-League lockfile."""
    try:
        parts = path.read_text().strip().split(":")
    except OSError:
        return False
    return len(parts) >= 5 and parts[0].lower().startswith("leagueclient")


def _find_lockfile() -> pathlib.Path | None:
    """Locate the live lockfile. Returns None if the client isn't running
    (lockfile absent — a normal, expected state, e.g. during a patch).

    Order: explicit override -> standard path -> search the mount root (only
    if the install was relocated by a patch). Raises LCUMountError if the
    mount root isn't visible at all (broken volume / wrong LCU_MOUNT_ROOT).
    """
    override = os.environ.get("LCU_LOCKFILE")
    if override:
        p = pathlib.Path(override)
        return p if p.is_file() else None

    root = _mount_root()
    if not root.exists():
        raise LCUMountError(
            f"mount root {root} is not visible in the container. The volume "
            f"mount is broken or League was moved — check the docker-compose "
            f"volume and LCU_MOUNT_ROOT."
        )

    # Fast path: the standard location under the mounted parent.
    default = root / "League of Legends" / "lockfile"
    if default.is_file() and _is_league_lockfile(default):
        return default

    # Slow path: search the mount root. Only reached when the file isn't at
    # the usual place — i.e. a patch relocated the install, or client closed.
    for candidate in root.rglob("lockfile"):
        if _is_league_lockfile(candidate):
            return candidate
    return None


def read_lockfile(path: pathlib.Path | None = None) -> tuple[int, str]:
    """Return (port, password) from the client lockfile.

    Format: ProcessName:PID:port:password:protocol
    """
    if path is None:
        path = _find_lockfile()
    if path is None or not path.is_file():
        raise LCUError(
            "lockfile not found — the League client isn't running. "
            "(This is normal when League is closed, e.g. during a patch.)"
        )
    # Re-read may race with the client writing at launch; retry once on a
    # malformed read before giving up.
    for attempt in range(2):
        parts = path.read_text().strip().split(":")
        if len(parts) >= 5:
            return int(parts[2]), parts[3]
        if attempt == 0:
            time.sleep(0.2)
    raise LCUError(f"unexpected lockfile format: {parts}")


class LCUClient:
    def __init__(self, lockfile: str | None = None):
        self._lockfile = pathlib.Path(lockfile) if lockfile else None
        self._session: aiohttp.ClientSession | None = None
        self._base = ""

    async def __aenter__(self):
        proxy_url = os.environ.get("LCU_PROXY_URL")
        if proxy_url:
            # Running in Docker: use the host-side lcu_proxy.py (plain HTTP, no auth).
            secret = os.environ.get("LCU_PROXY_SECRET", "")
            headers = {"X-Proxy-Auth": secret} if secret else {}
            self._session = aiohttp.ClientSession(headers=headers)
            self._base = proxy_url.rstrip("/")
        else:
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
            if r.status == 503:
                body = await r.json(content_type=None)
                raise LCUError(body.get("lcu_error", f"503 for {path}"))
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
                "totalDamageTaken": st.get("totalDamageTaken", 0),
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
