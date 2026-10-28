#!/bin/sh
# Entry point for the single Railway service (Lumen + core-fx + Firefox VPN core).
#
# Lifecycle (one deployment, one integrated lifecycle):
#   1. Prepare the persistent /data volume and make it writable as lumen.
#   2. Start the Firefox VPN core process (firefox-vpn-core) as a subprocess.
#   3. Start core-fx (uvicorn on CORE_FX_PORT) as a subprocess.
#   4. Start Lumen (uvicorn on PORT) in the foreground as the primary process.
#      All processes share one process tree so tini's signal forwarding reaches all.
#   5. On SIGTERM, tini forwards to all children; we gracefully stop all processes.
#
# Railway sends SIGTERM on shutdown; tini (PID 1) forwards it to all children.
# The entrypoint script traps signals and ensures graceful shutdown of all children.
set -eu

# Trap signals and forward to all child processes
children=""
cleanup() {
    echo "Received signal, stopping all processes..."
    for pid in $children; do
        kill -TERM "$pid" 2>/dev/null || true
    done
    wait
    exit 0
}
trap cleanup TERM INT

if [ "$(id -u)" = "0" ]; then
    mkdir -p /data
    chown -R lumen:lumen /data

    # Start Firefox VPN core process
    gosu lumen /app/core/firefox-vpn-core -data-dir /data/firefox-vpn -plain &
    CORE_PID=$!
    children="$children $CORE_PID"

    # Start core-fx (management panel) on internal port
    gosu lumen uvicorn core_fx:app --host 127.0.0.1 --port 8081 --log-level info &
    CORE_FX_PID=$!
    children="$children $CORE_FX_PID"

    # Start Lumen (main app) as the primary foreground process
    exec gosu lumen uvicorn main:app --host 0.0.0.0 --port 8080 --log-level info
fi

exec "$@"
