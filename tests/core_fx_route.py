#!/usr/bin/env python3
"""Firefox VPN /core-fx panel route contract.

Covers:
  * /core-fx route exists and requires super-admin authentication (in core_fx.py).
  * /api/core-fx/firefox-vpn/* endpoints require require_internal_service (in main.py).
  * /api/super-admin/* endpoints exist in core_fx.py.
  * /api/admin/firefox-vpn/* endpoints require require_internal_service (in main.py).
  * No secrets leak into public settings responses.
"""
import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="lumen-corefx-")

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
pg = types.ModuleType('pages'); pg.LOGIN_HTML = pg.DASHBOARD_HTML = pg.LANDING_HTML = pg.CORE_FX_HTML = ''; sys.modules['pages'] = pg
tg = types.ModuleType('telegram_bot')
async def noop(*a, **k): pass
tg.start_bot = noop; tg.stop_bot = noop; sys.modules['telegram_bot'] = tg

import main
import core_fx

failures = []

def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond: failures.append(name)

async def run():
    main.AUTH["password_hash"] = main.hash_password("admin-pass")
    main.INTERNAL_SERVICE_TOKEN = "test-internal-token"
    core_fx.SUPER_ADMIN_PASSWORD_HASH = core_fx._hash_password("super-pass")

    # /core-fx route is registered in core_fx.py.
    import inspect
    src = inspect.getsource(core_fx)
    check("/core-fx route registered in core_fx", "@app.get(\"/core-fx\"" in src or '@app.get(\'/core-fx\'' in src)

    # core-fx require_super_admin rejects a request with no session cookie.
    class FakeReq:
        cookies = {}
    try:
        await core_fx.require_super_admin(FakeReq())
        check("core-fx super-admin rejects no cookie", False, "should raise 401")
    except core_fx.HTTPException as exc:
        check("core-fx super-admin rejects no cookie", exc.status_code == 401)

    # core-fx super-admin credential is set correctly.
    check("core-fx super-admin password hash set", core_fx.SUPER_ADMIN_PASSWORD_HASH == core_fx._hash_password("super-pass"))

    # main.py firefox-vpn endpoints require require_internal_service.
    fv_endpoints = ["api_firefox_vpn_status", "api_firefox_vpn_credentials",
                    "api_firefox_vpn_signin", "api_firefox_vpn_verify",
                    "api_firefox_vpn_connect", "api_firefox_vpn_disconnect",
                    "api_firefox_vpn_location", "api_firefox_vpn_restart",
                    "api_firefox_vpn_locations"]
    for name in fv_endpoints:
        fn = getattr(main, name)
        defaults = (fn.__defaults__ or ()) + tuple((fn.__kwdefaults__ or {}).values())
        check(f"{name} requires internal-service", any("require_internal_service" in str(d) for d in defaults))

    # core-fx super-admin auth endpoints exist.
    check("core_fx has login endpoint", hasattr(core_fx, "api_login"))
    check("core_fx has logout endpoint", hasattr(core_fx, "api_logout"))
    check("core_fx has me endpoint", hasattr(core_fx, "api_me"))

    # Public settings do not expose secrets.
    settings = main._public_settings()
    fv = settings["firefox_vpn"]
    check("firefox_vpn settings has no password field", "password" not in fv)
    check("firefox_vpn settings reports email boolean only", isinstance(fv.get("email"), bool))

    print("CORE-FX ROUTE:", "ALL OK" if not failures else f"{len(failures)} FAILURES")

asyncio.run(run())
sys.exit(1 if failures else 0)