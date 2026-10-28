# ═══════════════════════════════════════════════════════════════════════════
# Lumen + Firefox VPN core — single Railway service
# ═══════════════════════════════════════════════════════════════════════════
#
# Multi-stage build:
#   1. builder   — compiles the Firefox VPN Go core (linux/amd64).
#   2. runtime   — Python 3.13 with the Lumen app, the compiled core binary,
#                  and an entrypoint that supervises both as one lifecycle.
#
# One deployment, one configuration surface, one integrated lifecycle:
#   /docker-entrypoint.sh   prepares /data, then runs Lumen's uvicorn in the
#                             foreground. Lumen's startup event starts the
#                             Firefox VPN core; its shutdown event stops it.
#   Railway SIGTERM → tini → Lumen shutdown → core stop.
#
# Build context must contain BOTH repositories:
#   <context>/              Lumen sources (this Dockerfile, main.py, …)
#   <context>/core/         Firefox VPN Go core sources (copied from the
#                           firefox-vpn-client repo: core/go.mod, core/cmd/…)
#
#   cp -r firefox-vpn-client-main/core lumen-checkout/core
#   docker build -t lumen-firefox-vpn lumen-checkout
#
# Required Railway variables:
#   PORT                    public HTTP port (default 8080)
#   ADMIN_PASSWORD          Lumen admin panel password
#   FIREFOX_VPN_EMAIL       administrator's Firefox Account email
#   FIREFOX_VPN_PASSWORD    administrator's Firefox Account password  (Railway variable, never in image)
#   FIREFOX_VPN_LOCATION    default exit country code, e.g. DE  (empty = recommended)
#   TELEGRAM_BOT_TOKEN      optional Telegram bot token
#   TELEGRAM_ADMIN_IDS      comma-separated admin chat ids
#   DATA_DIR                persistent volume path (default /data)
#   RAILWAY_VOLUME_MOUNT_PATH / LUMEN_REQUIRE_PERSISTENT_STORAGE=1  volume checks
#
# Secrets never land in the image: FIREFOX_VPN_PASSWORD is a Railway variable
# and is persisted encrypted at rest in /data/firefox_vpn_secret.enc.

# ── Stage 1: build the Firefox VPN core (Go) ─────────────────────────────
FROM golang:1.27-bookworm AS builder

WORKDIR /build/core
# Only the core module is needed; desktop app + tun tooling are not part of
# the single-service integration.
COPY core/go.mod core/go.sum ./
RUN go mod download

COPY core/ ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
    -ldflags="-s -w" \
    -o /build/firefox-vpn-core \
    ./cmd/firefox-vpn-core

# ── Stage 2: runtime image ───────────────────────────────────────────────
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    PORT=8080 \
    CORE_FX_PORT=8081 \
    FIREFOX_VPN_DATA_DIR=/data/firefox-vpn \
    FIREFOX_VPN_BIN=/app/core/firefox-vpn-core

WORKDIR /app

# ca-certificates for TLS to Mozilla; gosu to drop privileges; tini as PID 1
# to forward Railway's termination signal to Python (and thus to the core).
RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates gosu tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 lumen \
    && useradd --uid 10001 --gid lumen --create-home --home-dir /app lumen \
    && install -d --owner=lumen --group=lumen --mode=0700 /data

# Python dependencies first (better layer caching).
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

# Application code (Lumen + this integration).
COPY --chown=lumen:lumen . /app
RUN chmod 0755 /app/docker-entrypoint.sh

# Compiled Firefox VPN core binary from the builder stage.
COPY --from=builder --chown=lumen:lumen /build/firefox-vpn-core /app/core/firefox-vpn-core

EXPOSE 8080 8081

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker-entrypoint.sh"]
CMD ["python", "main.py"]
