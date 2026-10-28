#!/usr/bin/env python3
"""Firefox VPN outbound routing contract.

Covers:
  * firefox_vpn is a distinct outbound mode (not an ordinary proxy).
  * firefox_vpn and an ordinary proxy are mutually exclusive on a link.
  * fail-closed: a firefox_vpn link never leaks to a direct connection when
    the core is unavailable unless the admin explicitly allows fallback.
  * make_link normalizes firefox_vpn without proxy fields.
"""
import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="lumen-fvpn-")

import types
class App:
    def __init__(self, *a, **k): pass
    def deco(self, *a, **k): return lambda f: f
    on_event = get = post = patch = delete = api_route = middleware = deco
    def add_middleware(self, *a, **k): pass
    def add_api_websocket_route(self, *a, **k): pass
class HTTPException(Exception):
    def __init__(self, status_code=500, detail=''): self.status_code = status_code; self.detail = detail
class Dummy: pass
class WebSocketDisconnect(Exception): pass
fa = types.ModuleType('fastapi')
fa.FastAPI = App; fa.Request = Dummy; fa.HTTPException = HTTPException; fa.WebSocket = Dummy
fa.WebSocketDisconnect = WebSocketDisconnect; fa.Depends = lambda f: f
responses = types.ModuleType('fastapi.responses')
for n in ['Response', 'HTMLResponse', 'JSONResponse', 'RedirectResponse']:
    setattr(responses, n, type(n, (), {'__init__': lambda self, *a, **k: None}))
cors = types.ModuleType('fastapi.middleware.cors'); cors.CORSMiddleware = Dummy
sys.modules.update({
    'fastapi': fa, 'fastapi.responses': responses, 'fastapi.middleware': types.ModuleType('fastapi.middleware'),
    'fastapi.middleware.cors': cors,
})
sys.modules['aiofiles'] = types.ModuleType('aiofiles')
httpx = types.ModuleType('httpx'); httpx.AsyncClient = Dummy; httpx.Limits = Dummy; httpx.Timeout = Dummy
sys.modules['httpx'] = httpx
uv = types.ModuleType('uvicorn'); uv.Config = Dummy; uv.Server = Dummy; sys.modules['uvicorn'] = uv
pg = types.ModuleType('pages'); pg.LOGIN_HTML = pg.DASHBOARD_HTML = pg.LANDING_HTML = ''; sys.modules['pages'] = pg
tg = types.ModuleType('telegram_bot')
async def noop(*a, **k): pass
tg.start_bot = noop; tg.stop_bot = noop; sys.modules['telegram_bot'] = tg

import main, outbound

failures = []

def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond: failures.append(name)

async def run():
    main.LINKS.clear(); main.SUBS.clear()
    # No core binary in test env -> core unavailable.
    check("core unavailable flag", not main.FIREFOX_VPN.is_available())

    # 1. make_link with firefox_vpn drops proxy fields (mutual exclusivity).
    uid, link = await main.make_link(
        label="fvpn test", exit_proxy_mode="firefox_vpn",
        proxy_id="some-proxy-id", custom_proxy="http://user:pass@host:port",
        firefox_vpn_location="DE",
    )
    check("firefox_vpn drops proxy_id", link["proxy_id"] == "")
    check("firefox_vpn drops custom_proxy", link["custom_proxy"] == "")
    check("firefox_vpn keeps location", link["firefox_vpn_location"] == "DE")
    check("firefox_vpn mode persisted", link["exit_proxy_mode"] == "firefox_vpn")

    # 2. resolve_exit_selection returns the core's SOCKS5 endpoint (or raises
    #    ProxyUnavailableError when the core is down).
    try:
        sel = await main.resolve_exit_selection(link)
        check("resolve firefox_vpn endpoint", sel is not None and sel["endpoint"].startswith("socks5://"))
        check("resolve firefox_vpn location", sel.get("location_id") == "DE")
    except outbound.ProxyUnavailableError as exc:
        check("resolve firefox_vpn unavailable (no core)", True)
        sel = None

    # 3. open_outbound on a firefox_vpn link with no core must fail closed
    #    (no silent direct fallback).
    try:
        await outbound.open_outbound("example.com", 443, link=link)
        check("open_outbound fail-closed when core down", False, "should have raised")
    except outbound.ProxyUnavailableError:
        check("open_outbound fail-closed when core down", True)

    # 4. normalize_exit_proxy accepts firefox_vpn and clears proxy fields.
    mode, pid, cp = await main.normalize_exit_proxy("firefox_vpn", "pid-1", "custom")
    check("normalize firefox_vpn clears proxy", mode == "firefox_vpn" and pid == "" and cp == "")

    # 5. link_uses_firefox_vpn / link_uses_proxy mutual exclusivity.
    check("link_uses_firefox_vpn true", outbound.link_uses_firefox_vpn(link) is True)
    check("link_uses_proxy false on fvpn", outbound.link_uses_proxy(link) is False)
    check("link_exit_is_exclusive on fvpn", outbound.link_exit_is_exclusive(link) is True)
    direct_link = {"exit_proxy_mode": "direct", "proxy_id": ""}
    check("direct link exclusive", outbound.link_exit_is_exclusive(direct_link) is True)

    print("FIREFOX VPN ROUTING:", "ALL OK" if not failures else f"{len(failures)} FAILURES")

asyncio.run(run())
sys.exit(1 if failures else 0)
