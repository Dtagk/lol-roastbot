"""Run on the host: .venv\\Scripts\\python.exe lcu_proxy.py

Listens on 0.0.0.0:LCU_PROXY_PORT (default 58888) and proxies requests to the
local LCU HTTPS API (which only listens on 127.0.0.1). Lets the Dockerised bot
reach the League client without needing a volume mount or host networking.
"""
import asyncio, base64, os, pathlib, ssl
from aiohttp import web, ClientSession, TCPConnector

LOCKFILE = pathlib.Path(os.environ.get(
    "LCU_LOCKFILE_HOST",
    r"C:\Riot Games\League of Legends\lockfile",
))
PORT = int(os.environ.get("LCU_PROXY_PORT", 58888))


def _lcu_creds() -> tuple[int, str]:
    parts = LOCKFILE.read_text().strip().split(":")
    return int(parts[2]), parts[3]


async def proxy(request: web.Request) -> web.Response:
    try:
        lcu_port, pw = _lcu_creds()
    except (FileNotFoundError, OSError, ValueError, IndexError):
        return web.Response(
            status=503, content_type="application/json",
            text='{"lcu_error": "lockfile not found — League client not running"}',
        )
    token = base64.b64encode(f"riot:{pw}".encode()).decode()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    async with ClientSession(connector=TCPConnector(ssl=ctx)) as s:
        url = f"https://127.0.0.1:{lcu_port}{request.path_qs}"
        async with s.request(
            request.method, url,
            headers={"Authorization": f"Basic {token}"},
        ) as r:
            return web.Response(
                status=r.status, body=await r.read(),
                content_type="application/json",
            )


app = web.Application()
app.router.add_route("*", "/{path_info:.*}", proxy)

if __name__ == "__main__":
    print(f"LCU proxy listening on 0.0.0.0:{PORT} → {LOCKFILE}")
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)
