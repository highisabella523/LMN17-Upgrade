# Lumen + Firefox VPN Core Integration

**Single Railway service running Lumen's FastAPI application and the Firefox VPN Go core.**

## Overview

This repository integrates the Firefox VPN Go core (from
https://github.com/armanlife/firefox-vpn-client) into the Lumen FastAPI
reverse-proxy/service as **one deployable application**. Both run in a single
Docker image under a single process supervisor (tini → uvicorn → Lumen → core
subprocess).

## What changed

| File | Purpose |
|---|---|
| `Dockerfile` | Multi-stage: compiles the Go core, runs the Python app |
| `docker-entrypoint.sh` | Single entrypoint; runs as unprivileged `lumen` user |
| `main.py` | Core lifecycle, admin API, settings, routing |
| `outbound.py` | `firefox_vpn` outbound mode, fail-closed, mutual exclusivity |
| `firefox_vpn.py` | Core process manager + Mozilla dashboard API client |
| `vpn_secrets.py` | Encrypted at-rest credential storage |
| `pages.py` | Firefox VPN mode in the admin link-creation UI; `/core-fx` panel HTML |
| `telegram_bot.py` | `source: "telegram"`, default exit + dial address |
| `core/` | Firefox VPN Go source (unmodified upstream) |
| `requirements.txt` | Added `cryptography>=42.0.0` |

## Directory structure

```
.                                      <- Lumen application
core/                                  <- Firefox VPN Go source (upstream, unmodified)
  go.mod      Go module definition (go 1.27.2)
  go.sum      dependency checksums
  cmd/firefox-vpn-core/   program entrypoint
  internal/   service, proxy, fxa, guardian, config, accounts, ...
  doc/        operator, dashboard, configuration guides
tests/      contract tests (standalone scripts)
railway-installer/  separate Railway installer service (not part of this image)
```

## Build

```bash
docker build -t lumen-firefox-vpn .
```

The build context must include `core/` (the Firefox VPN Go source) at the
repository root — it is present in this archive.

## Deploy (Railway)

1. Create a Railway service from this folder (Docker build).
2. Mount a Railway Volume at `/data`.
3. Set environment variables:

| Variable | Example | Required |
|---|---|---|
| `PORT` | `8080` | yes |
| `ADMIN_PASSWORD` | `…` | yes |
| `FIREFOX_VPN_EMAIL` | `admin@example.com` | yes (for VPN exit) |
| `FIREFOX_VPN_PASSWORD` | `…` | yes (Railway variable only) |
| `FIREFOX_VPN_LOCATION` | `DE` | no (empty = recommended) |
| `TELEGRAM_BOT_TOKEN` | `…` | no |
| `TELEGRAM_ADMIN_IDS` | `…` | no |
| `DATA_DIR` | `/data` | no |
| `LUMEN_REQUIRE_PERSISTENT_STORAGE` | `1` | no |

4. Deploy. The admin panel is at `/dashboard`; the Firefox VPN section is
   there for credentials, sign-in, exit location, connect/disconnect/restart.

## Configuration

- Firefox VPN credentials are entered once in the admin panel and persisted
  encrypted at rest in `/data/firefox_vpn_secret.enc` (mode 0600). They are
  never in the image, never in `code_state.json`, never returned by any API.
- Exit location is chosen per-link or globally (`FIREFOX_VPN_LOCATION` env or
  admin panel). The admin panel verifies the location against the core's
  server list.
- Telegram users inherit `TELEGRAM_DEFAULT_EXIT` and `TELEGRAM_DIAL_ADDRESS`
  automatically; existing users keep their settings unless the explicit
  migration tool is used.

## Firefox VPN admin panel (`/core-fx`)

A dedicated super-admin panel is available at `https://<domain>/core-fx` for managing the Firefox VPN core:

* **Route:** `GET /core-fx` — HTML management page (super-admin only).
* **Auth:** Separate super-admin credential (`SUPER_ADMIN_PASSWORD`, falls back to `ADMIN_PASSWORD`). Verified via `/api/super-admin/me`, `/api/super-admin/login`, `/api/super-admin/logout`.
* **APIs (all require super-admin):**
  * `GET /api/admin/firefox-vpn/status` — core status, account, exit location.
  * `POST /api/admin/firefox-vpn/credentials` — save email + password (encrypted at rest).
  * `POST /api/admin/firefox-vpn/signin` — start Mozilla sign-in.
  * `POST /api/admin/firefox-vpn/verify` — submit email verification code.
  * `POST /api/admin/firefox-vpn/connect` / `disconnect` — toggle proxy.
  * `POST /api/admin/firefox-vpn/location` — select exit location.
  * `POST /api/admin/firefox-vpn/restart` — restart core engine.
  * `GET /api/admin/firefox-vpn/locations` — available exit locations.

A super-admin nav link appears in the dashboard sidebar only when the super-admin session is active.

## Tests

Run from the repository root:

```bash
python tests/firefox_vpn_routing.py
python tests/admin_settings_contract.py
python tests/core_fx_route.py
python tests/ml_contract.py
python tests/state_persistence_v28.py
python tests/store_bot_contract.py
# ... plus the other tests/ scripts
```

## Limitations

- Docker image build was not performed here (Docker unavailable in this
  environment).
- Live Firefox VPN / Mozilla integration was not performed (no real Firefox
  Account credentials, no network to Mozilla).
- Railway deploy was not performed.

See `ARCHITECTURAL-DECISIONS-FIREFOX-VPN.md` for the full decision log and
limitation list.

## License

- Lumen application code: per the Lumen repository's license.
- Firefox VPN Go core (`core/`): GNU GPL v3 (upstream).