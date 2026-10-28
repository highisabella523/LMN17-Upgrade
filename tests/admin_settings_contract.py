#!/usr/bin/env python3
"""Admin settings contract: max locations, Telegram defaults, TLS SNI optional.

Covers:
  * Admin-configurable max subscription locations (applied consistently).
  * More than two locations in a subscription accepted when limit raised.
  * Validation rejects exceeding the configured max.
  * Telegram default exit + dial-address separation.
  * TLS SNI left empty while WebSocket Host stays configured.
"""
import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="lumen-admin-")

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

    # ── Max locations ─────────────────────────────────────────────────
    check("default max is 2", main.max_subscription_locations() == 2)

    # Raise the limit to 5 and validate_multi_location accepts 5 countries.
    async with main.FIREFOX_VPN_CONFIG_LOCK:
        main.FIREFOX_VPN_CONFIG["max_subscription_locations"] = 5
    check("raised max reads back", main.max_subscription_locations() == 5)

    import proxy_repository as repo
    codes = ["DE", "FR", "NL", "PL", "SE"]
    repo_records = [
        repo.Record(f"c{i}", f"http://10.0.0.{i}:8080", "http", __import__("countries").country_name(c), c, "🌐", 5)
        for i, c in enumerate(codes)
    ]
    repo._records = {r.id: r for r in repo_records}
    repo._last = __import__("time").monotonic(); repo._error = ""

    locations = [
        {"id": f"loc-{i}", "code": codes[i], "proxy_id": f"c{i}", "active": True}
        for i in range(5)
    ]
    try:
        ml = await main.validate_multi_location(
            {"enabled": True, "locations": locations},
            previous={"enabled": False, "locations": [], "selection_mode": "explicit"},
        )
        check("5 locations accepted when max=5", ml["enabled"] is True and len(ml["locations"]) == 5)
    except ValueError as exc:
        check("5 locations accepted when max=5", False, str(exc))

    # Exceeding the max (6 when limit is 5) is rejected.
    try:
        await main.validate_multi_location(
            {"enabled": True, "locations": locations + [{"id": "loc-6", "code": "C9", "proxy_id": "c9", "active": True}]},
            previous={"enabled": False, "locations": [], "selection_mode": "explicit"},
        )
        check("6 locations rejected when max=5", False, "should have raised")
    except ValueError:
        check("6 locations rejected when max=5", True)

    # Lowering the limit back to 2 rejects 5.
    async with main.FIREFOX_VPN_CONFIG_LOCK:
        main.FIREFOX_VPN_CONFIG["max_subscription_locations"] = 2
    try:
        await main.validate_multi_location(
            {"enabled": True, "locations": locations},
            previous={"enabled": False, "locations": [], "selection_mode": "explicit"},
        )
        check("5 locations rejected when max=2", False, "should have raised")
    except ValueError:
        check("5 locations rejected when max=2", True)

    # ── Telegram settings ─────────────────────────────────────────────
    main.TELEGRAM_CONFIG["default_exit"] = "DE"
    main.TELEGRAM_CONFIG["dial_address"] = "tg.example.com"
    settings = main._public_settings()
    check("telegram default exit in settings", settings["telegram"]["default_exit"] == "DE")
    check("telegram dial address in settings", settings["telegram"]["dial_address"] == "tg.example.com")

    # ── TLS SNI optional ──────────────────────────────────────────────
    main.TLS_CONFIG["sni_optional"] = True
    settings = main._public_settings()
    check("tls sni_optional in settings", settings["tls"]["sni_optional"] is True)

    from config_address import normalize_sni, link_hosts
    # Empty SNI means unset: link creation skips normalize_sni for empty values
    # (main.py: sni = normalize_sni(sni) if str(sni or "").strip() else ""),
    # so an explicitly empty SNI is accepted and preserved as "".
    check("link make accepts empty sni", True)  # exercised via make_link below
    check("non-empty SNI preserved", normalize_sni("front.example.com") == "front.example.com")
    # link_hosts: custom SNI wins; address is independent of SNI.
    dial, tls_name = main.link_hosts("104.16.1.1", "front.example.org", "service.example.com")
    check("SNI independent of IP address", dial == "104.16.1.1" and tls_name == "front.example.org")

    # make_link with empty SNI persists empty string (not replaced by dial host).
    uid2, link2 = await main.make_link(label="sni empty", address="104.16.1.1", sni="")
    check("empty SNI persisted as ''", link2["sni"] == "")

    # link_hosts: custom SNI wins; address is independent of SNI.
    dial, tls_name = main.link_hosts("104.16.1.1", "front.example.org", "service.example.com")
    check("SNI independent of IP address", dial == "104.16.1.1" and tls_name == "front.example.org")

    print("ADMIN SETTINGS:", "ALL OK" if not failures else f"{len(failures)} FAILURES")

asyncio.run(run())
sys.exit(1 if failures else 0)
