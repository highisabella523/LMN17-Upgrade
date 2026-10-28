# Architectural Decisions & Limitations — Firefox VPN Core Integration

## Integration method

**Decision:** The Firefox VPN Go core runs as a **subprocess of the same Lumen
process**, not as a separate Railway service. Lumen's `startup` event starts
`firefox-vpn-core` (`-data-dir /data/firefox-vpn -plain`) and its `shutdown`
event stops it. tini (PID 1) forwards Railway's SIGTERM to uvicorn, which
triggers FastAPI's shutdown → core stop.

**Why:** The task mandates one deployment, one configuration surface, one
integrated lifecycle. The core is a long-lived daemon with its own SOCKS5
proxy (127.0.0.1:10810) and dashboard API (127.0.0.1:10820); Lumen supervises
it and server-to-server-calls its loopback API. Both bind only loopback ports,
so neither management interface is exposed publicly.

**Alternative considered:** A sidecar container in the same Railway service.
Rejected because the Go core's data plane (SOCKS5 listener) is reached by
Lumen's outbound connector over localhost; two containers would need shared
networking and complicate graceful shutdown ordering.

## Build

**Decision:** Multi-stage Dockerfile. Stage 1 (`golang:1.27-bookworm`) compiles
`core/cmd/firefox-vpn-core` to a static binary (`CGO_ENABLED=0`). Stage 2
(`python:3.13-slim`) installs Lumen's Python deps and copies the binary.

**Limitation:** The build context must contain both the Lumen checkout and the
Firefox VPN `core/` directory (see deployment guide). Go and Docker were not
available in this development environment, so the image was not built here.
The Python side was import- and contract-tested; the Dockerfile is written to
the multi-stage requirement and reviewed for correctness.

## Authentication & credentials

**Decision:** Reuse the core's real FxA flow. The admin enters email +
password in the Lumen panel; Lumen calls the core's `POST /api/signin`. Any
email verification code is supplied by the admin through the panel
(`POST /api/signin/{id}`). The core stores OAuth tokens + password in its own
`user-data/` folder. Lumen persists only email + password, **encrypted at
rest** in `/data/firefox_vpn_secret.enc` (mode 0600), so it can re-sign-in on
restart without the admin re-entering them.

- `cryptography` (Fernet) is used when installed; a stdlib-only PBKDF2/XOR
  fallback is used otherwise (obfuscation-grade at rest; credentials never in
  logs/API responses/source).
- The upstream password is **never** stored in `code_state.json`, never
  returned by any API, never logged, never in the image (Railway variable only).

**Limitation:** FxA sign-in may require an email verification code. The core
supports this (verification_method email-2fa), but the admin must paste the
code into the panel. There is no way to fully automate sign-in without either
the code or pre-stored OAuth refresh tokens in the core's `user-data/`.

**Limitation:** Mozilla may require re-authentication (`ErrReauthRequired`)
or refuse a location (`location_refused`). Lumen surfaces `auth_error` /
`wake_error_code` in the panel but does not fabricate a successful login.

## Exit location selection

**Decision:** Reuse `POST /api/location` (country code, `"fastest"`, or `""`
for recommended). The verified exit country/city is read from the core's
`GET /api/status` (exit probe) and from the connection test. Lumen stores
`firefox_vpn_location` per link and in `FIREFOX_VPN_CONFIG`.

**Distinction:** Lumen keeps the location *label* separate from the verified
exit IP/country (`exit_country_code`, `exit_country_name`, `exit_city`) —
requirement 7 explicitly demands this distinction.

**Limitation:** Location availability is verified against the core's live
server list (`GET /api/locations`) and the exit probe. If Mozilla does not
let this account use a location, the core reports `location_refused` and
Lumen shows it as an auth error.

## Outbound routing

**Decision:** `firefox_vpn` is a **distinct outbound type** (`outbound.FIREFOX_VPN_MODE`)
in `outbound.py`, alongside `direct`, `repository`, and `custom`. A firefox_vpn
link routes through the core's local SOCKS5 listener. It is **mutually
exclusive** with an ordinary proxy: `normalize_exit_proxy` clears proxy fields
for firefox_vpn, and `link_exit_is_exclusive` enforces it.

**Fail-closed:** When the core is unavailable, `open_outbound` raises
`ProxyUnavailableError` for a firefox_vpn link — no silent fallback to a
direct connection (requirement 9). Direct and ordinary-proxy links are
unaffected.

**Limitation:** Firefox VPN carries TCP only (no UDP), and Mozilla connects
only to ports 80/443/8080/8443 (per the upstream README). Traffic to other
ports fails through the core — this is an upstream limitation, documented in
the upstream README's "Known limitations".

## Multi-location limit

**Decision:** Removed the hard-coded `ML_REQUIRED_LOCATIONS = 2`. Replaced with
`max_subscription_locations()`, an admin-configurable persisted value (default
2 for backward compatibility, cap 64). Applied consistently in
`validate_multi_location`, `resolve_exit_selection`, the panel, and API.

**Migration:** When the admin lowers the limit, existing Multi-Location groups
with more locations are marked `migration_required` on load so the panel can
offer a safe migration instead of failing closed.

## Telegram users

**Decision:** Telegram-originated users are distinguished by `source: "telegram"`
in `telegram_store.json` (set at first `_ensure_user`). Never inferred from
name/email/username patterns. New Telegram users inherit the admin-configured
`TELEGRAM_CONFIG.default_exit` (default `DE`) and `dial_address`, applied
automatically in `_provision_new`. Existing users keep their settings unless
the admin runs the explicit migration tool
(`POST /api/admin/telegram/migration-apply`, requires `migrate_existing=true`).

**Dial-address policy:** The Telegram dial address is configured once in the
panel; new Telegram links get it in their `address` field. Migration updates
only the dial address / default-exit fields — WebSocket Host, path, and other
fields are untouched.

**Pre-purchase trial:** Not implemented. The existing bot's `trial` plan is a
paid-plan-equivalent free service after payment (`amount == 0`), not a true
pre-purchase restricted subscription. The requirement notes this: if a genuine
pre-purchase trial needs temporary credentials, an additional bot workflow is
necessary (documented below).

## TLS SNI

**Decision:** The TLS SNI field may be left empty / unset (`normalize_sni("")`
returns `""`; link creation skips `normalize_sni` for empty values). Empty SNI
is **not** replaced with the dial hostname. WebSocket Host and path remain
independently configurable via `link_hosts(address, sni, service_host)`.

**Security:** Empty SNI does **not** disable certificate verification or
hostname validation. That decision belongs to the client; Lumen only serializes
the VLESS URI. The requirement's "compare success/latency with and without SNI"
is left to the operator (no benchmark claims made).

**Limitation:** If a specific transport cannot safely support empty SNI, that
transport's adapter should reject it. The current native transports (vless-ws,
vless-tcp) pass SNI through to the client; no transport in this build blocks
empty SNI.

## Persistence

**Decision:** All new settings (`FIREFOX_VPN_CONFIG`, `TELEGRAM_CONFIG`,
`TLS_CONFIG`, `max_subscription_locations`) are stored in `code_state.json`
alongside existing state, with the same atomic-write / backup / snapshot
mechanism. The upstream password lives in the encrypted sidecar
(`vpn_secrets.py`), not in state.

Migrations are repeatable and safe: `load_state` tolerates missing keys, and
`save_state` is atomic (tmp + fsync + rename) with backups.

**Secrets:** Configuration export endpoints never return the upstream password
or sidecar contents.

## Authorization

**Decision:** All new admin endpoints (`/api/admin/firefox-vpn/*`,
`/api/admin/settings`, `/api/admin/telegram/migration-*`) require
`require_auth` (authenticated admin session cookie). The core's own dashboard
API is called server-to-server using the local token; it is never exposed to
the browser or end users.

## What could not be verified in this environment

- **Docker image build:** Go and Docker are not installed here. The Dockerfile
  is written and reviewed but not built.
- **Live Firefox VPN core / Mozilla:** No real Firefox Account credentials and
  no network to Mozilla's FxA/Guardian/proxy servers. All core integration is
  exercised through contract tests that assert fail-closed behavior and mode
  exclusivity without a running core. The "authentication and connection
  status" and "test outbound connection through Firefox VPN" acceptance items
  are **blocked** on real credentials/network and must be run with a real
  Firefox Account against Mozilla.
- **Railway deploy:** Not performed. The deployment guide documents the
  required variables and volume.
