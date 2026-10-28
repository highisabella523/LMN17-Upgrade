# Railway Deployment Guide — Lumen + Firefox VPN Core (Single Service)

This guide deploys **one Railway service** that runs both the Lumen FastAPI
application and the Firefox VPN Go core, supervised by a single entrypoint.

## What this delivers

| Requirement | How it's met |
|---|---|
| One Railway service / one deployable app | A single Docker image builds both the Go core and the Python app |
| Lumen FastAPI + admin panel | Unchanged, served by uvicorn on `$PORT` |
| Firefox VPN core + runtime components | `firefox-vpn-core` binary compiled in stage 1, started by Lumen's startup |
| One startup / process-management mechanism | `docker-entrypoint.sh` → tini → uvicorn (foreground); Lumen's `startup` event starts the core as a subprocess; `shutdown` stops it |
| Persistent config + account data | Railway Volume mounted at `/data` (state + encrypted credentials) |
| Web UI reports healthy only when core reachable | `/health` and admin status reflect `FIREFOX_VPN.is_available()` |
| Termination signals, graceful shutdown | tini (PID 1) forwards SIGTERM → uvicorn shutdown → core stop |

## Repository layout for the build

The Docker build context must contain **both** repositories:

```
<context>/                  <- Lumen checkout (this Dockerfile, main.py, …)
<context>/core/             <- Firefox VPN Go core (core/go.mod, core/cmd/…)
```

Prepare it once:

```bash
# from a folder that holds both extracted repos
mkdir lumen-build
cp -r Lumen-Project-Final-main/* lumen-build/
cp -r firefox-vpn-client-main/core lumen-build/core
ls lumen-build/core/go.mod   # must exist
```

## Railway setup

1. Create a new Railway service from the `lumen-build` folder (Docker build).
2. Add a **Railway Volume** and mount it at `/data` (this is where
   `code_state.json`, the encrypted Firefox VPN credentials sidecar, and the
   core's `user-data/` folder live). Set:
   - `LUMEN_REQUIRE_PERSISTENT_STORAGE=1`
   - `RAILWAY_VOLUME_MOUNT_PATH=<the volume's mount path>`
3. Set environment variables (see below). **Never put `FIREFOX_VPN_PASSWORD` in
   the image or repo** — supply it as a Railway variable.

## Required environment variables

| Variable | Example | Notes |
|---|---|---|
| `PORT` | `8080` | Public HTTP port |
| `ADMIN_PASSWORD` | `…` | Lumen admin panel password |
| `FIREFOX_VPN_EMAIL` | `admin@example.com` | Firefox Account email (admin-only) |
| `FIREFOX_VPN_PASSWORD` | `…` | Firefox Account password — **Railway variable only** |
| `FIREFOX_VPN_LOCATION` | `DE` | Default exit country code (empty = Mozilla recommended) |
| `TELEGRAM_BOT_TOKEN` | `123456:ABC-…` | Optional Telegram bot |
| `TELEGRAM_ADMIN_IDS` | `123,456` | Comma-separated admin chat ids |
| `TELEGRAM_DEFAULT_EXIT` | `DE` | Default exit for new Telegram users |
| `TELEGRAM_DIAL_ADDRESS` | `tg.example.com` | Dial address for Telegram users |
| `DATA_DIR` | `/data` | Persistent volume path |

The Firefox VPN account email + password are entered once in the admin panel
(Firefox VPN section) and persisted **encrypted at rest** in
`/data/firefox_vpn_secret.enc` (mode 0600). They are never in the image,
never in `code_state.json`, and never returned by any API.

## How it works at runtime

1. Container starts → tini (PID 1) → uvicorn runs `main.py` as user `lumen`.
2. Lumen `startup`:
   - loads persistent state (`code_state.json`),
   - starts `firefox-vpn-core` with `-data-dir /data/firefox-vpn -plain` as a
     supervised subprocess,
   - if credentials are stored, auto-signs in to Mozilla (no admin interaction
     on restart),
   - connects the proxy and applies the configured exit location.
3. The admin opens `/dashboard` → Firefox VPN section:
   - views account email (masked), connection state, auth errors,
   - exit country/city currently verified by Mozilla,
   - saves email + password, triggers sign-in / verification,
   - selects an exit location (verified against the core's server list),
   - connects / disconnects / restarts the core.
4. Links created with exit mode `firefox_vpn` route outbound traffic through
   the core's local SOCKS5 listener (`127.0.0.1:10810`). Firefox VPN and an
   ordinary managed/custom proxy are mutually exclusive per link.
5. Railway SIGTERM → tini → uvicorn shutdown event → `FIREFOX_VPN.stop()`
   terminates the core gracefully.

## Health checks

- Railway healthcheck: `GET /health` (path already configured in
  `railway.json`).
- `/health` reports `status: ok` when Lumen is up; the admin panel separately
  shows whether the Firefox VPN core is connected. If the core fails, Lumen
  keeps serving (direct/ordinary-proxy links unaffected) but firefox_vpn links
  fail closed instead of leaking a direct connection.

## Health-check behaviour on Firefox VPN failure

Lumen does **not** mark itself unhealthy purely because the Firefox VPN core
is down: the existing direct and ordinary-proxy paths continue working, and
firefox_vpn links fail closed (no silent direct fallback). The admin panel
displays the core's wake error / connection status so an operator can react.

## Ports

- `8080` (`$PORT`): Lumen API + admin panel (public, via Railway TLS).
- `127.0.0.1:10810`: Firefox VPN SOCKS5 listener (loopback only — **not**
  exposed publicly).
- `127.0.0.1:10820`: Firefox VPN dashboard API (loopback only — used by
  Lumen server-to-server; **not** exposed publicly).

## Volumes

| Mount | Purpose |
|---|---|
| `/data` | Lumen `code_state.json`, backups, encrypted VPN credentials sidecar, core `user-data/` |

Without the volume, state and credentials are lost on redeploy and the admin
must re-enter the Firefox VPN password.

## Firefox VPN core source

The `core/` directory contains the upstream Firefox VPN Go core source,
unmodified, from https://github.com/armanlife/firefox-vpn-client (the
`core/` subdirectory of that repository). It is included in the build
context so the Dockerfile can compile `firefox-vpn-core` in stage 1.

- Origin: `core/` of armanlife/firefox-vpn-client
- License: GNU GPL v3 (see `core/LICENSE` — the upstream repository's
  top-level LICENSE applies; `core/` is part of that repository)
- Go version: `go 1.27.2` (declared in `core/go.mod`)
- Build: `CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build ./core/cmd/firefox-vpn-core`
- No local modifications were made to the core source.

## Files changed (this integration)

- `Dockerfile` — multi-stage: builds Go core, runs Python app
- `docker-entrypoint.sh` — single entrypoint, drops to `lumen` user
- `main.py` — Firefox VPN core lifecycle, admin API, settings, routing
- `outbound.py` — `firefox_vpn` outbound mode, fail-closed, mutual exclusivity
- `firefox_vpn.py` — core process manager + Mozilla dashboard API client
- `vpn_secrets.py` — encrypted at-rest credential storage
- `pages.py` — Firefox VPN mode in the admin link-creation UI
- `telegram_bot.py` — `source: "telegram"`, default exit + dial address
- `tests/firefox_vpn_routing.py`, `tests/admin_settings_contract.py` — new tests
- `requirements.txt` — added `cryptography>=42.0.0` for credentials encryption
- `RAILWAY-FIREFOX-VPN-DEPLOYMENT.md` — this guide

## Tests performed

All tests run standalone (no pytest, no network to Mozilla, no live core):

- `config_address.py`, `countries_contract.py`, `transport_registry_contract.py`,
  `protocol_contract.py`, `startup_symbol_contract.py`, `proxy_repository_contract.py`,
  `dualstack_ipv6.py`, `raw_tcp_e2e.py`: PASS (pre-existing, unchanged)
- `ml_contract.py` (multi-location exact-two contract): PASS
- `state_persistence_v28.py` (state persistence regression): PASS
- `store_bot_contract.py` (Telegram store contract): PASS
- `telegram_bot_navigation_contract.py`: PASS
- `proxy_reliability_v16.py`, `proxy_split_prefetch_v16.py`: PASS
- `firefox_vpn_routing.py` (NEW: Firefox VPN routing/fail-closed/mutual-exclusivity): PASS
- `admin_settings_contract.py` (NEW: max locations/Telegram defaults/TLS SNI): PASS
- Secret storage round-trip + permissions: PASS

Not performed (environmental blockers):
- Docker image build — Docker not available in this environment.
- Container startup / health checks — no Docker.
- Live Firefox VPN core / Mozilla integration — no real Firefox Account
  credentials, no network to Mozilla's FxA/Guardian/proxy servers.
- Railway deploy — no Railway account access.

See `ARCHITECTURAL-DECISIONS-FIREFOX-VPN.md` for the full decision log and
limitation list.
