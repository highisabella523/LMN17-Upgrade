"""Firefox VPN Core Management Application (core-fx).

Standalone web application that runs alongside Lumen in the same Railway
service. It provides the super-admin panel for managing the Firefox VPN
core (authentication, exit location, connection state) through a dedicated
HTTP port (internal, loopback-only by default).

Lumen exposes the internal Firefox VPN API at /api/admin/firefox-vpn/*,
authenticated by INTERNAL_SERVICE_TOKEN. core-fx calls those endpoints
server-to-server and renders its own UI, completely separate from Lumen's
frontend. No shared JavaScript, CSS, or DOM between the two apps.
"""

from __future__ import annotations

import asyncio
import httpx
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import Response, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import hashlib
import hmac

logger = logging.getLogger("core_fx")

app = FastAPI(title="Lumen core-fx", docs_url=None, redoc_url=None)

# ── Configuration ──────────────────────────────────────────────────────────

# Internal port for core-fx (loopback-only; exposed to the public via reverse
# proxy at /core-fx).
CORE_FX_PORT = int(os.environ.get("CORE_FX_PORT", "8081"))
CORE_FX_HOST = os.environ.get("CORE_FX_HOST", "127.0.0.1")

# Lumen's public port (used to build reverse-proxy target URLs if needed).
LUMEN_PORT = int(os.environ.get("PORT", "8000"))

# Shared secret for core-fx -> Lumen internal API calls. Must match Lumen's
# INTERNAL_SERVICE_TOKEN. Never exposed to browsers.
INTERNAL_SERVICE_TOKEN = os.environ.get("INTERNAL_SERVICE_TOKEN", "").strip()

# Super-admin credential for the core-fx login page.
SUPER_ADMIN_COOKIE = "core_fx_session"
SESSION_TTL = 60 * 60 * 24 * 365
SUPER_ADMIN_PASSWORD_HASH: str | None = None


def _init_super_admin() -> None:
    global SUPER_ADMIN_PASSWORD_HASH
    pw = os.environ.get("SUPER_ADMIN_PASSWORD", "").strip()
    if pw:
        SUPER_ADMIN_PASSWORD_HASH = hashlib.sha256(f"{pw}{os.environ.get('SECRET_KEY', 'core-fx')}".encode()).hexdigest()
    else:
        # Fall back to the Lumen admin password hash so existing deployments keep
        # working without a separate super-admin credential.
        lumen_pw = os.environ.get("ADMIN_PASSWORD", "123456")
        lumen_secret = os.environ.get("SECRET_KEY", "")
        SUPER_ADMIN_PASSWORD_HASH = hashlib.sha256(f"{lumen_pw}{lumen_secret}".encode()).hexdigest()


_init_super_admin()

SESSIONS: dict = {}
SESSIONS_LOCK = asyncio.Lock()

# Where Lumen lives (same container, loopback). core-fx calls Lumen's internal
# Firefox VPN API here.
LUMEN_BASE = os.environ.get("LUMEN_INTERNAL_URL", f"http://{CORE_FX_HOST}:{LUMEN_PORT}")

# ── HTTP client (shared, reusable) ─────────────────────────────────────────

_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    return _http_client


# ── Auth ───────────────────────────────────────────────────────────────────

def _hash_password(pw: str) -> str:
    return hashlib.sha256(f"{pw}{os.environ.get('SECRET_KEY', 'core-fx')}".encode()).hexdigest()


async def create_session() -> str:
    token = secrets.token_urlsafe(32)
    async with SESSIONS_LOCK:
        SESSIONS[token] = time.time() + SESSION_TTL
    return token


async def is_valid_session(token: str | None) -> bool:
    if not token:
        return False
    async with SESSIONS_LOCK:
        exp = SESSIONS.get(token)
        if exp is None:
            return False
        if exp < time.time():
            SESSIONS.pop(token, None)
            return False
        return True


async def destroy_session(token: str | None) -> None:
    if not token:
        return
    async with SESSIONS_LOCK:
        SESSIONS.pop(token, None)


async def require_super_admin(request: Request) -> str:
    token = request.cookies.get(SUPER_ADMIN_COOKIE)
    if not await is_valid_session(token):
        raise HTTPException(status_code=401, detail="super-admin authentication required")
    return token


async def require_internal_service(request: Request) -> None:
    """Require a valid internal service token for Lumen -> core-fx calls."""
    token = request.headers.get("x-internal-service-token", "").strip()
    if not INTERNAL_SERVICE_TOKEN or not token or not hmac.compare_digest(token, INTERNAL_SERVICE_TOKEN):
        raise HTTPException(status_code=401, detail="internal service authentication required")


# ── Lumen internal API client ──────────────────────────────────────────────

async def _lumen_get(path: str) -> dict[str, Any]:
    """Call Lumen's internal Firefox VPN API (authenticated by service token)."""
    client = _get_http_client()
    try:
        resp = await client.get(
            f"{LUMEN_BASE}{path}",
            headers={"X-Internal-Service-Token": INTERNAL_SERVICE_TOKEN},
        )
        if resp.status_code // 100 == 2:
            return resp.json()
        return {"_error": resp.status_code, "_body": resp.text[:200]}
    except Exception as exc:  # Lumen unreachable
        return {"_error": "exception", "_body": str(exc)[:200]}


async def _lumen_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    client = _get_http_client()
    try:
        resp = await client.post(
            f"{LUMEN_BASE}{path}",
            headers={
                "Content-Type": "application/json",
                "X-Internal-Service-Token": INTERNAL_SERVICE_TOKEN,
            },
            json=payload,
        )
        if resp.status_code // 100 in (2, 3):
            return resp.json()
        return {"_error": resp.status_code, "_body": resp.text[:200]}
    except Exception as exc:
        return {"_error": "exception", "_body": str(exc)[:200]}


# ── Routes ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict[str, Any]:
    """Health check. Reports whether core-fx itself is up (Lumen reachability
    is checked separately by the UI)."""
    return {"status": "ok", "service": "core-fx"}


@app.get("/api/core-fx/me")
async def api_me(request: Request) -> dict[str, bool]:
    return {"authenticated": await is_valid_session(request.cookies.get(SUPER_ADMIN_COOKIE))}


@app.post("/api/core-fx/login")
async def api_login(request: Request) -> Response:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid body")
    pw = str(body.get("password", "")).strip()
    if _hash_password(pw) != SUPER_ADMIN_PASSWORD_HASH:
        raise HTTPException(status_code=401, detail="super-admin password is incorrect")
    token = await create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        SUPER_ADMIN_COOKIE, token, max_age=SESSION_TTL, httponly=True,
        secure=True, samesite="lax", path="/",
    )
    return resp


@app.post("/api/core-fx/logout")
async def api_logout(request: Request) -> Response:
    await destroy_session(request.cookies.get(SUPER_ADMIN_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SUPER_ADMIN_COOKIE, path="/")
    return resp


# ── Firefox VPN management (proxied to Lumen's internal API) ──────────────

@app.get("/api/core-fx/firefox-vpn/status")
async def api_firefox_vpn_status(_=Depends(require_internal_service)) -> dict[str, Any]:
    return await _lumen_get("/api/admin/firefox-vpn/status")


@app.post("/api/core-fx/firefox-vpn/credentials")
async def api_firefox_vpn_credentials(request: Request, _=Depends(require_internal_service)) -> dict[str, Any]:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid body")
    return await _lumen_post("/api/admin/firefox-vpn/credentials", body)


@app.post("/api/core-fx/firefox-vpn/signin")
async def api_firefox_vpn_signin(_=Depends(require_internal_service)) -> dict[str, Any]:
    return await _lumen_post("/api/admin/firefox-vpn/signin", {})


@app.post("/api/core-fx/firefox-vpn/verify")
async def api_firefox_vpn_verify(request: Request, _=Depends(require_internal_service)) -> dict[str, Any]:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid body")
    return await _lumen_post("/api/admin/firefox-vpn/verify", body)


@app.post("/api/core-fx/firefox-vpn/connect")
async def api_firefox_vpn_connect(_=Depends(require_internal_service)) -> dict[str, Any]:
    return await _lumen_post("/api/admin/firefox-vpn/connect", {})


@app.post("/api/core-fx/firefox-vpn/disconnect")
async def api_firefox_vpn_disconnect(_=Depends(require_internal_service)) -> dict[str, Any]:
    return await _lumen_post("/api/admin/firefox-vpn/disconnect", {})


@app.post("/api/core-fx/firefox-vpn/location")
async def api_firefox_vpn_location(request: Request, _=Depends(require_internal_service)) -> dict[str, Any]:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid body")
    return await _lumen_post("/api/admin/firefox-vpn/location", body)


@app.post("/api/core-fx/firefox-vpn/restart")
async def api_firefox_vpn_restart(_=Depends(require_internal_service)) -> dict[str, Any]:
    return await _lumen_post("/api/admin/firefox-vpn/restart", {})


@app.get("/api/core-fx/firefox-vpn/locations")
async def api_firefox_vpn_locations(_=Depends(require_internal_service)) -> dict[str, Any]:
    return await _lumen_get("/api/admin/firefox-vpn/locations")


# ── Internal health endpoint (for Lumen -> core-fx checks) ─────────────────

@app.get("/api/core-fx/internal/health")
async def api_internal_health(_=Depends(require_internal_service)) -> dict[str, str]:
    return {"status": "ok", "service": "core-fx"}


# ── UI ─────────────────────────────────────────────────────────────────────

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en" dir="ltr">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>core-fx · Sign in</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.19.0/dist/tabler-icons.min.css">
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;overflow:hidden}
body{font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','SF Pro Text','Inter','Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;background:#0E1213;color:#F2F3F3;display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:#151A1B;border:1px solid #3A4246;border-radius:20px;padding:38px 34px 34px;backdrop-filter:blur(24px);box-shadow:0 0 80px rgba(24,155,173,0.07),0 20px 60px rgba(0,0,0,.5)}
.brand{display:flex;align-items:center;gap:14px;margin-bottom:28px}
.brand-img{width:48px;height:48px;border-radius:50%;overflow:hidden;border:1px solid #3A4246;box-shadow:0 0 20px rgba(168,53,28,0.35),0 0 12px rgba(24,155,173,0.3);flex-shrink:0}
.brand-img img{width:100%;height:100%;object-fit:cover}
.brand-name{font-size:16px;font-weight:700;color:#F2F3F3}
.brand-sub{font-size:11px;color:#8C9192;margin-top:2px}
h1{font-size:21px;font-weight:700;color:#F2F3F3;margin-bottom:5px;letter-spacing:-.02em}
.sub{font-size:12px;color:#8C9192;margin-bottom:24px;line-height:1.6}
.field{margin-bottom:18px}
.field label{display:block;font-size:10.5px;font-weight:600;color:#8C9192;margin-bottom:7px;text-transform:uppercase;letter-spacing:.06em}
.inp-wrap{position:relative}
input[type=password]{width:100%;padding:13px 16px 13px 44px;border-radius:11px;border:1px solid #3A4246;background:#00000033;color:#F2F3F3;font-family:inherit;font-size:14px;outline:none;transition:.2s}
input[type=password]:focus{border-color:#189BAD71;background:#0000004d;box-shadow:0 0 0 3px rgba(24,155,173,.1)}
.ic{position:absolute;left:14px;top:50%;transform:translateY(-50%);color:#8C9192;font-size:18px;pointer-events:none;transition:.2s}
input:focus+.ic{color:#189BAD}
.err{display:none;background:rgba(179,58,34,.08);border:1px solid rgba(179,58,34,.2);border-radius:10px;padding:10px 14px;margin-bottom:14px;font-size:12px;color:#E08167;align-items:center;gap:8px}
.err.show{display:flex}
.btn{width:100%;padding:13px;border-radius:11px;border:none;cursor:pointer;font-family:inherit;font-size:14px;font-weight:600;display:inline-flex;align-items:center;justify-content:center;gap:8px;transition:.2s;position:relative;overflow:hidden}
.btn-primary{background:linear-gradient(135deg,#12A2B5,#A8351C);color:#fff;box-shadow:0 4px 20px rgba(168,53,28,.35)}
.btn-primary:hover{filter:brightness(1.05);transform:translateY(-1px)}
.btn:disabled{opacity:.55;cursor:not-allowed}
.footer{margin-top:22px;padding-top:18px;border-top:1px solid #3A4246;display:flex;align-items:center;justify-content:center;gap:8px;font-size:11px;color:#8C9192}
.footer a{color:#189BAD;font-weight:600;text-decoration:none;display:flex;align-items:center;gap:4px}
@keyframes spin{to{transform:rotate(360deg)}}
.aspin{display:inline-block;position:relative;width:18px;height:18px;color:currentColor;vertical-align:-4px}
.aspin b{position:absolute;top:0;left:50%;width:2px;height:5px;margin-left:-1px;border-radius:2px;background:currentColor;transform-origin:1px 9px;animation:aspin-fade 1s linear infinite;opacity:.12}
.aspin b:nth-child(1){transform:rotate(0deg);animation-delay:-0.917s}
.aspin b:nth-child(2){transform:rotate(30deg);animation-delay:-0.833s}
.aspin b:nth-child(3){transform:rotate(60deg);animation-delay:-0.750s}
.aspin b:nth-child(4){transform:rotate(90deg);animation-delay:-0.667s}
.aspin b:nth-child(5){transform:rotate(120deg);animation-delay:-0.583s}
.aspin b:nth-child(6){transform:rotate(150deg);animation-delay:-0.500s}
.aspin b:nth-child(7){transform:rotate(180deg);animation-delay:-0.417s}
.aspin b:nth-child(8){transform:rotate(210deg);animation-delay:-0.333s}
.aspin b:nth-child(9){transform:rotate(240deg);animation-delay:-0.250s}
.aspin b:nth-child(10){transform:rotate(270deg);animation-delay:-0.167s}
.aspin b:nth-child(11){transform:rotate(300deg);animation-delay:-0.083s}
.aspin b:nth-child(12){transform:rotate(330deg);animation-delay:-0.000s}
@keyframes aspin-fade{0%{opacity:1}100%{opacity:.12}}
</style>
</head>
<body>
<div class="card">
  <div class="brand">
    <div class="brand-img"><i class="ti ti-signal-cellular-secure"></i></div>
    <div>
      <div class="brand-name">core-fx</div>
      <div class="brand-sub">Firefox VPN Core Management · super-admin</div>
    </div>
  </div>
  <h1>Unlock core management</h1>
  <p class="sub">A separate credential from the Lumen admin password. Controls Mozilla account credentials.</p>
  <div class="err" id="err"><i class="ti ti-alert-circle"></i><span id="err-text"></span></div>
  <form id="form">
    <div class="field">
      <label>Super-admin password</label>
      <div class="inp-wrap">
        <input type="password" id="pw" placeholder="Enter your password" autofocus required>
        <i class="ti ti-lock ic"></i>
      </div>
    </div>
    <button class="btn btn-primary" type="submit" id="btn"><i class="ti ti-login-2"></i><span>Sign in</span></button>
  </form>
  <div class="footer"><i class="ti ti-shield-lock"></i> Encrypted connection · super-admin only</div>
</div>
<script>
document.getElementById('form').addEventListener('submit',async e=>{
  e.preventDefault();
  const btn=document.getElementById('btn'),err=document.getElementById('err'),et=document.getElementById('err-text');
  err.classList.remove('show');btn.disabled=true;
  btn.innerHTML='<span class="aspin"><b></b><b></b><b></b><b></b><b></b><b></b><b></b><b></b><b></b><b></b><b></b><b></b></span> Signing in...';
  try{
    const r=await fetch('/api/core-fx/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('pw').value})});
    if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.detail||'Error');}
    location.href='/';
  }catch(e){
    et.textContent=e.message;err.classList.add('show');
    btn.disabled=false;btn.innerHTML='<i class="ti ti-login-2"></i><span>Sign in</span>';
  }
});
</script>
</body></html>"""


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en" dir="ltr">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>core-fx · Firefox VPN Core</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.19.0/dist/tabler-icons.min.css">
<style>
*{box-sizing:border-box}html,body{margin:0;min-height:100%}
:root{color-scheme:dark;--surface:#0E1213;--low:#151A1B;--mid:#1C2325;--high:#27292F;--text:#F2F3F3;--muted:#8C9192;--outline:#3A4246;--ok:#12A08B;--ok-bg:rgba(18,160,139,.12);--warn:#C08A2E;--warn-bg:rgba(192,138,46,.12);--err:#B33A22;--err-bg:rgba(179,58,34,.12);--accent:#189BAD;--shape:16px}
body{font-family:'Vazirmatn','Inter','Segoe UI',Arial,sans-serif;color:var(--text);background:var(--surface);padding:24px}
.wrap{max-width:980px;margin:0 auto}
.bar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:24px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:10px}
.mark{width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,#394777,#189BAD);display:grid;place-items:center;color:#fff;font-weight:800}
.brand-name{font-size:15px;font-weight:700;letter-spacing:-.02em}
.brand-sub{font-size:11px;color:var(--muted)}
.actions{display:flex;gap:8px;flex-wrap:wrap}
.btn{border:none;border-radius:10px;padding:9px 16px;cursor:pointer;font-family:inherit;font-size:13px;font-weight:600;display:inline-flex;align-items:center;gap:7px;transition:.2s}
.btn i{font-size:14px}
.btn-primary{background:linear-gradient(135deg,#394777,#189BAD);color:#fff;box-shadow:0 4px 14px rgba(24,155,173,.35)}
.btn-primary:hover{filter:brightness(1.05);transform:translateY(-1px)}
.btn-ghost{background:var(--mid);color:var(--text);border:1px solid var(--outline)}
.btn-ghost:hover{background:var(--high);border-color:var(--accent)}
.btn-danger{background:var(--err-bg);color:var(--err);border:1px solid rgba(179,58,34,.25)}
.btn-danger:hover{background:rgba(179,58,34,.2)}
.btn:disabled{opacity:.55;cursor:not-allowed}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:22px}
.card{background:var(--low);border:1px solid var(--outline);border-radius:var(--shape);padding:18px 20px}
.card .k{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);font-weight:700;margin-bottom:10px;display:flex;align-items:center;gap:8px}
.card .k i{font-size:13px}
.card .v{font-size:21px;font-weight:700;letter-spacing:-.02em}
.card .sub{font-size:11.5px;color:var(--muted);margin-top:6px}
.ok-chip{display:inline-flex;align-items:center;gap:6px;background:var(--ok-bg);color:var(--ok);font-size:11px;font-weight:700;border-radius:999px;padding:4px 10px;border:1px solid rgba(18,160,139,.25)}
.err-chip{display:inline-flex;align-items:center;gap:6px;background:var(--err-bg);color:var(--err);font-size:11px;font-weight:700;border-radius:999px;padding:4px 10px;border:1px solid rgba(179,58,34,.25)}
.warn-chip{display:inline-flex;align-items:center;gap:6px;background:var(--warn-bg);color:var(--warn);font-size:11px;font-weight:700;border-radius:999px;padding:4px 10px;border:1px solid rgba(192,138,46,.25)}
.section{background:var(--low);border:1px solid var(--outline);border-radius:var(--shape);margin-bottom:18px;overflow:hidden}
.sec-head{display:flex;align-items:center;justify-content:space-between;padding:16px 18px;border-bottom:1px solid var(--outline);background:color-mix(in srgb,var(--mid) 8%,transparent)}
.sec-title{font-size:13px;font-weight:700;letter-spacing:-.01em;display:flex;align-items:center;gap:8px}
.sec-title i{color:var(--accent);font-size:15px}
.sec-body{padding:18px}
.row{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px;align-items:flex-end}
.field{display:flex;flex-direction:column;gap:7px;flex:1;min-width:170px}
.field label{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
input,select{width:100%;padding:10px 14px;border-radius:10px;border:1px solid var(--outline);background:var(--mid);color:var(--text);font-family:inherit;font-size:13px;outline:none;transition:.15s}
input:focus,select:focus{border-color:var(--accent);background:color-mix(in srgb,var(--high) 90%,var(--surface) 10%)}
.two{grid-template-columns:1fr 1fr}
.msg{margin:10px 0;padding:12px 16px;border-radius:10px;font-size:13px;font-weight:600;display:none;align-items:center;gap:10px}
.msg i{font-size:15px}
.msg.ok{background:var(--ok-bg);color:var(--ok);border:1px solid rgba(18,160,139,.25)}
.msg.err{background:var(--err-bg);color:var(--err);border:1px solid rgba(179,58,34,.25)}
.msg.warn{background:var(--warn-bg);color:var(--warn);border:1px solid rgba(192,138,46,.25)}
.loc-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.loc-item{border:1px solid var(--outline);border-radius:10px;padding:12px 14px;background:var(--mid);cursor:pointer;transition:.15s;display:flex;flex-direction:column;gap:6px}
.loc-item:hover{border-color:var(--accent);background:color-mix(in srgb,var(--high) 80%,var(--surface) 20%)}
.loc-item.selected{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 12%,transparent)}
.loc-item .c{font-size:11px;font-weight:700}
.loc-item .n{font-size:12px;color:var(--text)}
.loc-item .s{font-size:10px;color:var(--muted)}
.footer{margin-top:26px;padding-top:18px;border-top:1px solid var(--outline);color:var(--muted);font-size:11px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
.small{font-size:11.5px;color:var(--muted)}
</style>
</head>
<body>
<div class="wrap">
<div class="bar">
<div class="brand">
<div class="mark"><i class="ti ti-signal-cellular-secure"></i></div>
<div><div class="brand-name">core-fx</div><div class="brand-sub">Firefox VPN Core Management · super-admin</div></div>
</div>
<div class="actions">
<button class="btn btn-ghost" onclick="refreshAll()"><i class="ti ti-refresh"></i><span>Refresh</span></button>
<button class="btn btn-danger" onclick="doLogout()"><i class="ti ti-logout"></i><span>Sign out</span></button>
</div>
</div>

<div id="auth-box">
<div class="card" style="max-width:420px;margin:0 auto">
<div class="sec-head"><div class="sec-title"><i class="ti ti-lock"></i> Super-admin authentication</div></div>
<div class="sec-body" style="text-align:center">
<div class="msg msg-err" id="auth-err"><i class="ti ti-alert-circle"></i><span id="auth-err-text"></span></div>
<div class="field"><label>Super-admin password</label><input type="password" id="pw" placeholder="Enter super-admin password"></div>
<button class="btn btn-primary" style="width:100%" onclick="doLogin()"><i class="ti ti-login-2"></i><span>Unlock core management</span></button>
<div class="small" style="margin-top:12px">Separate from the Lumen admin password. Controls Mozilla account credentials.</div>
</div>
</div>
</div>

<div id="app-box" style="display:none">
<div class="grid">
<div class="card">
<div class="k"><i class="ti ti-shield-check"></i> Core status</div>
<div class="v" id="stat-phase">—</div>
<div class="sub" id="stat-message">Unknown</div>
</div>
<div class="card">
<div class="k"><i class="ti ti-account-circle"></i> Account</div>
<div class="v" id="stat-account">—</div>
<div class="sub" id="stat-account-status">No account configured</div>
</div>
<div class="card">
<div class="k"><i class="ti ti-map-pin"></i> Exit location</div>
<div class="v" id="stat-exit">—</div>
<div class="sub" id="stat-exit-detail">Not verified</div>
</div>
<div class="card">
<div class="k"><i class="ti ti-shield-lock"></i> Credentials</div>
<div class="v" id="stat-creds">Not set</div>
<div class="sub">Stored encrypted at rest</div>
</div>
</div>

<div class="section">
<div class="sec-head"><div class="sec-title"><i class="ti ti-mail"></i> Account credentials</div></div>
<div class="sec-body">
<div class="msg msg-ok" id="cred-msg-ok"><i class="ti ti-check"></i><span id="cred-msg-ok-text"></span></div>
<div class="msg msg-err" id="cred-msg-err"><i class="ti ti-alert-circle"></i><span id="cred-msg-err-text"></span></div>
<div class="row two">
<div class="field"><label>Firefox Account email</label><input type="email" id="fx-email" placeholder="you@example.com"></div>
<div class="field"><label>Password</label><input type="password" id="fx-pw" placeholder="••••••••"></div>
</div>
<div class="row" style="margin-top:8px">
<button class="btn btn-primary" onclick="saveCreds()"><i class="ti ti-save"></i><span>Save credentials</span></button>
<button class="btn btn-ghost" onclick="startSignIn()"><i class="ti ti-key"></i><span>Sign in to Mozilla</span></button>
<button class="btn btn-ghost" onclick="doConnect()"><i class="ti ti-power"></i><span>Connect proxy</span></button>
<button class="btn btn-ghost" onclick="doDisconnect()"><i class="ti ti-power-off"></i><span>Disconnect</span></button>
</div>
<div class="row" style="margin-top:10px">
<div class="field" style="flex:1;min-width:260px"><label>Verification code (from email)</label><input type="text" id="fx-code" placeholder="123456"></div>
<button class="btn btn-ghost" onclick="verifySignIn()"><i class="ti ti-check-circle"></i><span>Verify code</span></button>
</div>
<div class="small">The password is stored encrypted at rest and never returned by any API. Mozilla may send a confirmation code to your account email.</div>
</div>
</div>

<div class="section">
<div class="sec-head"><div class="sec-title"><i class="ti ti-globe"></i> Exit location</div></div>
<div class="sec-body">
<div class="row">
<div class="field" style="flex:1"><label>Location</label><select id="fx-location"><option value="">Loading locations…</option></select></div>
<button class="btn btn-primary" onclick="saveLocation()"><i class="ti ti-save"></i><span>Save location</span></button>
<button class="btn btn-ghost" onclick="loadLocations()"><i class="ti ti-refresh"></i><span>Refresh list</span></button>
</div>
<div class="loc-list" id="loc-list">—</div>
<div class="small">Select an exit country. The core verifies that traffic actually leaves from the chosen country.</div>
</div>
</div>

<div class="section">
<div class="sec-head"><div class="sec-title"><i class="ti ti-terminal-box"></i> Diagnostics</div></div>
<div class="sec-body">
<div class="row">
<div class="card" style="flex:1"><div class="k"><i class="ti ti-file-type-js"></i> Core binary</div><div class="v" id="diag-bin">Checking…</div></div>
<div class="card" style="flex:1"><div class="k"><i class="ti ti-cpu"></i> Core process</div><div class="v" id="diag-proc">Checking…</div></div>
<div class="card" style="flex:1"><div class="k"><i class="ti ti-network"></i> SOCKS5 outbound</div><div class="v" id="diag-socks">Checking…</div></div>
</div>
<button class="btn btn-ghost" onclick="runDiagnostics()"><i class="ti ti-stethoscope"></i><span>Run diagnostics</span></button>
<button class="btn btn-ghost" onclick="doRestart()"><i class="ti ti-rotate"></i><span>Restart core</span></button>
<div class="small">Core binary: /app/core/firefox-vpn-core · SOCKS5: 127.0.0.1:10810 · Dashboard API: 127.0.0.1:10820 (loopback only)</div>
</div>
</div>

<div class="footer"><span>core-fx · Firefox VPN integration</span><span id="last-upd">—</span></div>
</div>

<script>
async function api(path,opts={}){
  const r=await fetch(path,{...opts,credentials:'same-origin',headers:{'Content-Type':'application/json',...(opts.headers||{})}});
  return r;
}
async function json(path,opts={}){
  const r=await api(path,opts);
  let d;try{d=await r.json()}catch(e){d={}}
  return {ok:r.ok,status:r.status,data:d,detail:d.detail||''}
}

async function checkAuth(){
  try{
    const r=await json('/api/core-fx/me');
    document.getElementById('auth-box').style.display=r.ok?'none':'block';
    document.getElementById('app-box').style.display=r.ok?'block':'none';
    if(!r.ok)return;
    await loadAll();
  }catch(e){
    document.getElementById('auth-box').style.display='block';
    document.getElementById('app-box').style.display='none';
    show('auth-err','auth-err-text','Unable to reach authentication service.','err');
  }
}

async function doLogin(){
  const pw=document.getElementById('pw').value;
  const err=document.getElementById('auth-err');err.style.display='none';
  try{
    const r=await api('/api/core-fx/login',{method:'POST',body:JSON.stringify({password:pw})});
    if(!r.ok){const d=await r.json().catch(()=>({}));document.getElementById('auth-err-text').textContent=d.detail||'Login failed';err.style.display='flex';return}
    location.reload();
  }catch(e){
    document.getElementById('auth-err-text').textContent='Could not reach login service.';
    err.style.display='flex';
  }
}
async function doLogout(){
  try{await api('/api/core-fx/logout',{method:'POST'});}catch(e){}
  location.reload();
}

async function loadAll(){
  await loadStatus();
  await loadLocations();
  await runDiagnostics();
  document.getElementById('last-upd').textContent='Updated · '+new Date().toLocaleTimeString();
}

async function loadStatus(){
  try{
    const r=await json('/api/core-fx/firefox-vpn/status');
    if(!r.ok){show('cred-msg-err','cred-msg-err-text',r.detail||'Could not load status','err');return}
    const c=r.data.config||{},core=r.data.core||{};
    document.getElementById('stat-phase').textContent=c.connected?'Connected':(core.message||'Unknown');
    document.getElementById('stat-phase').className=c.connected?'ok-chip':'warn-chip';
    document.getElementById('stat-message').textContent=core.message||'—';
    document.getElementById('stat-account').textContent=c.email?c.email:'Not set';
    document.getElementById('stat-account-status').textContent=c.auth_error||(c.connected?'Authenticated':'No credentials');
    document.getElementById('stat-account-status').className=c.auth_error?'err-chip':(c.connected?'ok-chip':'warn-chip');
    document.getElementById('stat-creds').textContent=c.email?'Configured (encrypted)':'Not set';
    document.getElementById('stat-creds').className=c.email?'ok-chip':'warn-chip';
    document.getElementById('stat-exit').textContent=c.location||'Recommended';
    document.getElementById('stat-exit-detail').textContent=c.exit_country_code?c.exit_country_name||c.exit_country_code:'Not verified';
    document.getElementById('fx-email').value=c.email||'';
    document.getElementById('fx-location').value=c.location||'';
  }catch(e){
    show('cred-msg-err','cred-msg-err-text','Status check failed: '+e.message,'err');
  }
}

async function loadLocations(){
  try{
    const r=await json('/api/core-fx/firefox-vpn/locations');
    const sel=document.getElementById('fx-location');const list=document.getElementById('loc-list');
    sel.innerHTML='';list.innerHTML='';
    if(!r.ok||!r.data.locations){sel.innerHTML='<option value="">Unavailable</option>';list.innerHTML='<div class="small">Could not load locations</div>';return}
    const locs=r.data.locations;
    let recAdded=false;
    for(const loc of locs){
      const opt=document.createElement('option');opt.value=loc.code;opt.textContent=(loc.recommended?'★ ':'')+(loc.name||loc.code)+(loc.city?' · '+loc.city:'');sel.appendChild(opt);
      const item=document.createElement('div');item.className='loc-item'+(loc.recommended?' selected':'');
      item.innerHTML='<div class="c">'+esc((loc.recommended?'★ ':'')+(loc.name||loc.code))+'</div><div class="n">'+esc(loc.city||'')+'</div><div class="s">'+esc(loc.recommended?'Recommended':'Server · '+(loc.server||'—'))+'</div>';
      item.onclick=()=>{sel.value=loc.code;document.querySelectorAll('.loc-item').forEach(x=>x.classList.remove('selected'));item.classList.add('selected')};
      list.appendChild(item);
      if(loc.recommended&&!recAdded){sel.value=loc.code;item.classList.add('selected');recAdded=true}
    }
  }catch(e){
    const list=document.getElementById('loc-list');list.innerHTML='<div class="small">Location list failed: '+e.message+'</div>';
  }
}

async function saveCreds(){
  const email=document.getElementById('fx-email').value.trim();const pw=document.getElementById('fx-pw').value;
  const okEl=document.getElementById('cred-msg-ok');const errEl=document.getElementById('cred-msg-err');
  okEl.style.display='none';errEl.style.display='none';
  if(!email||!pw){show('cred-msg-err','cred-msg-err-text','Email and password are required','err');return}
  try{
    const r=await json('/api/core-fx/firefox-vpn/credentials',{method:'POST',body:JSON.stringify({email,password:pw})});
    if(!r.ok){show('cred-msg-err','cred-msg-err-text',r.detail||'Save failed','err');return}
    document.getElementById('fx-pw').value='';
    show('cred-msg-ok','cred-msg-ok-text','Credentials saved (encrypted at rest)','ok');
    await loadStatus();
  }catch(e){show('cred-msg-err','cred-msg-err-text','Save failed: '+e.message,'err');}
}

async function startSignIn(){
  try{
    const r=await json('/api/core-fx/firefox-vpn/signin',{method:'POST'});
    if(!r.ok){show('cred-msg-err','cred-msg-err-text',r.detail||'Sign-in failed','err');return}
    if(r.data.verification_required){show('cred-msg-ok','cred-msg-ok-text','Verification code required — check your email','ok');}
    else{show('cred-msg-ok','cred-msg-ok-text','Signed in successfully','ok');}
    await loadStatus();
  }catch(e){show('cred-msg-err','cred-msg-err-text','Sign-in failed: '+e.message,'err');}
}

async function verifySignIn(){
  const id=prompt('Enter the sign-in session ID (returned by the core):');if(!id)return;
  const code=document.getElementById('fx-code').value.trim();if(!code){show('cred-msg-err','cred-msg-err-text','Enter the verification code','err');return}
  try{
    const r=await json('/api/core-fx/firefox-vpn/verify',{method:'POST',body:JSON.stringify({sign_in_id:id,code})});
    if(!r.ok){show('cred-msg-err','cred-msg-err-text',r.detail||'Verification failed','err');return}
    show('cred-msg-ok','cred-msg-ok-text','Verified — account connected','ok');
    await loadStatus();
  }catch(e){show('cred-msg-err','cred-msg-err-text','Verification failed: '+e.message,'err');}
}

async function doConnect(){
  try{
    const r=await json('/api/core-fx/firefox-vpn/connect',{method:'POST'});
    if(!r.ok){show('cred-msg-err','cred-msg-err-text',r.detail||'Connect failed','err');return}
    show('cred-msg-ok','cred-msg-ok-text','Connected to Mozilla','ok');
    await loadStatus();
  }catch(e){show('cred-msg-err','cred-msg-err-text','Connect failed: '+e.message,'err');}
}

async function doDisconnect(){
  try{
    const r=await json('/api/core-fx/firefox-vpn/disconnect',{method:'POST'});
    if(!r.ok){show('cred-msg-err','cred-msg-err-text',r.detail||'Disconnect failed','err');return}
    show('cred-msg-ok','cred-msg-ok-text','Disconnected','ok');
    await loadStatus();
  }catch(e){show('cred-msg-err','cred-msg-err-text','Disconnect failed: '+e.message,'err');}
}

async function saveLocation(){
  try{
    const c=document.getElementById('fx-location').value;
    const r=await json('/api/core-fx/firefox-vpn/location',{method:'POST',body:JSON.stringify({country:c})});
    if(!r.ok){show('cred-msg-err','cred-msg-err-text',r.detail||'Location save failed','err');return}
    show('cred-msg-ok','cred-msg-ok-text','Exit location saved','ok');
    await loadStatus();
  }catch(e){show('cred-msg-err','cred-msg-err-text','Location save failed: '+e.message,'err');}
}

async function doRestart(){
  if(!confirm('Restart the Firefox VPN core? Existing connections may drop briefly.'))return;
  try{
    const r=await json('/api/core-fx/firefox-vpn/restart',{method:'POST'});
    if(!r.ok){show('cred-msg-err','cred-msg-err-text',r.detail||'Restart failed','err');return}
    show('cred-msg-ok','cred-msg-ok-text','Restarting core…','ok');
    await loadStatus();
  }catch(e){show('cred-msg-err','cred-msg-err-text','Restart failed: '+e.message,'err');}
}

async function runDiagnostics(){
  const binEl=document.getElementById('diag-bin');const procEl=document.getElementById('diag-proc');const socksEl=document.getElementById('diag-socks');
  try{
    const r=await fetch('http://127.0.0.1:10820/api/status').then(()=>'reachable',()=>'unreachable');
    socksEl.textContent='SOCKS5 listener: '+(r==='reachable'?'reachable':'unreachable (core may be down)');
  }catch(e){socksEl.textContent='SOCKS5 listener: cannot probe';}
  try{
    const r=await json('/api/core-fx/firefox-vpn/status');
    binEl.textContent=r.data.core?.available?'Present':'Missing';
    procEl.textContent=r.data.core?.connected?'Running':'Not running';
  }catch(e){binEl.textContent='Unknown';procEl.textContent='Unknown';}
}

function show(id,textId,text,kind){
  const el=document.getElementById(id);const te=document.getElementById(textId);
  if(!el||!te)return;
  te.textContent=text;el.className='msg msg-'+kind;el.style.display='flex';
}

function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

checkAuth();
</script>
</body></html>"""


@app.get("/core-fx", response_class=HTMLResponse)
async def core_fx_root(request: Request):
    if not await is_valid_session(request.cookies.get(SUPER_ADMIN_COOKIE)):
        return HTMLResponse(content=LOGIN_HTML)
    return HTMLResponse(content=DASHBOARD_HTML)


@app.get("/", response_class=HTMLResponse)
async def core_fx_dashboard(request: Request):
    if not await is_valid_session(request.cookies.get(SUPER_ADMIN_COOKIE)):
        return HTMLResponse(content=LOGIN_HTML)
    return HTMLResponse(content=DASHBOARD_HTML)


@app.on_event("shutdown")
async def shutdown():
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None


if __name__ == "__main__":
    uvicorn.run("core_fx:app", host=CORE_FX_HOST, port=CORE_FX_PORT, log_level="info")
