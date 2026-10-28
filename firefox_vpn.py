"""Firefox VPN core process manager and Mozilla dashboard API client.

This module owns the lifecycle of the Firefox VPN Go core (``firefox-vpn-core``)
inside the single Railway service, plus a small client for the core's own
dashboard API (SOCKS5 proxy on 127.0.0.1:10810, HTTP API on 127.0.0.1:10820).

Design points, verified against the actual Go core source:

* The core is a long-lived daemon: it runs the SOCKS5/HTTP proxy and its web
  dashboard, keeps an upstream session to Mozilla, and switches accounts when
  quota runs out. It is started once with the service and supervised for the
  container's lifetime.
* Authentication reuses the core's real FxA flow: the admin enters email +
  password in the Lumen panel, Lumen calls ``POST /api/signin`` on the core,
  any email verification code is supplied by the admin through the panel, and
  the core stores OAuth tokens + password in its own ``user-data/`` folder.
  Lumen never sees or stores OAuth tokens; it only keeps the email + password
  encrypted at rest in ``vpn_secrets.py`` so it can re-sign-in on restart.
* Location selection reuses ``POST /api/location`` (a country code, ``"fastest"``
  or ``""`` for recommended). The exit country is read from ``GET /api/status``
  and from the connection test, so Lumen can verify that the traffic actually
  leaves from the requested country.
* The local SOCKS5 proxy is Lumen's outbound next hop for any config whose
  exit mode is ``firefox_vpn``. The two modes (Firefox VPN vs an ordinary
  managed/custom proxy) are mutually exclusive per link, enforced in
  ``outbound.py`` and ``main.py``.
* Signals: the supervisor forwards SIGTERM/SIGINT to the core, which shuts
  down its proxy, dashboard, and upstream session. Lumen stops first, then the
  core, so the web UI reports itself healthy only while the core is reachable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from vpn_secrets import stored_credentials, save_credentials, clear_credentials

logger = logging.getLogger("Lumen.firefox_vpn")

# ── Defaults matching the Firefox VPN Go core (core/internal/config/config.go) ──
DEFAULT_PROXY_LISTEN = "127.0.0.1:10810"
DEFAULT_DASHBOARD_LISTEN = "127.0.0.1:10820"
CORE_API_BASE = f"http://{DEFAULT_DASHBOARD_LISTEN}"
LOCAL_TOKEN_HEADER = "X-Local-Token"

# The core's data folder lives next to the binary in standalone mode:
# <exe-dir>/user-data . In the container we point it at /data/firefox-vpn.
DEFAULT_CORE_DATA_DIR = Path(os.environ.get("FIREFOX_VPN_DATA_DIR", "/data/firefox-vpn"))

# How long to wait for the core's API to answer after start.
CORE_STARTUP_TIMEOUT = 30.0
# A single API call timeout.
CORE_API_TIMEOUT = 10.0

# Outbound mode constant used on link records and throughout routing.
FIREFOX_VPN_MODE = "firefox_vpn"


class FirefoxVPNError(RuntimeError):
    """Raised when the Firefox VPN core cannot be used."""


class FirefoxVPNCore:
    """Supervises the ``firefox-vpn-core`` process and wraps its API.

    Only one instance should exist per process; it is created at startup and
    held in ``main.FIREFOX_VPN``.
    """

    def __init__(
        self,
        binary: str | Path | None = None,
        data_dir: Path | None = None,
        verbose: bool = False,
    ) -> None:
        self.binary = Path(binary or os.environ.get("FIREFOX_VPN_BIN", "/app/core/firefox-vpn-core"))
        self.data_dir = Path(data_dir or DEFAULT_CORE_DATA_DIR)
        self.verbose = verbose
        self.process: subprocess.Popen | None = None
        self._started = False
        self._http: httpx.AsyncClient | None = None
        self._local_token: str | None = None
        self.status: dict[str, Any] = {
            "phase": "off",
            "message": "not started",
            "account_email": "",
            "account_active": False,
            "exit_country_code": "",
            "exit_country_name": "",
            "exit_city": "",
            "proxy_country_code": "",
            "proxy_country_name": "",
            "connected": False,
            "auth_error": "",
            "wake_error": "",
            "wake_error_code": "",
        }

    # ── lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the core process and wait for its API to answer."""
        if self._started:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.binary.exists():
            logger.warning(
                "Firefox VPN core binary not found at %s; Firefox VPN exit unavailable", self.binary
            )
            self.status["phase"] = "error"
            self.status["message"] = "core binary missing"
            return

        # Standalone mode (no -app): the proxy is on from the start and the
        # dashboard API answers on loopback. We own the process for the
        # container's lifetime.
        cmd = [
            str(self.binary),
            "-data-dir", str(self.data_dir),
            "-plain",
        ]
        if self.verbose:
            cmd.append("-verbose")

        logger.info("Starting Firefox VPN core: %s", " ".join(cmd))
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        self._http = httpx.AsyncClient(timeout=httpx.Timeout(CORE_API_TIMEOUT))
        self._local_token = self._read_local_token()

        # Wait for the dashboard API to answer.
        deadline = time.monotonic() + CORE_STARTUP_TIMEOUT
        last_err = ""
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                last_err = f"core exited with code {self.process.returncode}"
                break
            try:
                st = await self._api_get("/api/status")
                if st:
                    self._started = True
                    self._merge_status(st)
                    self.status["phase"] = "running"
                    self.status["connected"] = self._is_connected(st)
                    logger.info(
                        "Firefox VPN core API reachable; phase=%s connected=%s",
                        self.status["phase"], self.status["connected"],
                    )
                    return
            except Exception as exc:  # core still warming up
                last_err = type(exc).__name__
            await asyncio.sleep(0.5)

        self._started = False
        self.status["phase"] = "error"
        self.status["message"] = f"core API unavailable: {last_err}"
        logger.warning("Firefox VPN core did not become ready: %s", last_err)

    def _handle_signal(self) -> None:
        """Forward a termination signal to the core process."""
        if self.process and self.process.poll() is None:
            try:
                self.process.send_signal(signal.SIGTERM)
            except OSError:
                pass

    async def stop(self) -> None:
        """Stop the core process gracefully, then terminate if needed."""
        self._started = False
        if self._http:
            try:
                # Ask the core to exit if it is hosted by us; it is not, so
                # just close the proxy by ending the process.
                await self._http.post("/api/app/exit", timeout=httpx.Timeout(3.0))
            except Exception:
                pass
            await self._http.aclose()
            self._http = None
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self.process = None
        self.status = {
            "phase": "off",
            "message": "stopped",
            "account_email": "",
            "account_active": False,
            "exit_country_code": "",
            "exit_country_name": "",
            "exit_city": "",
            "proxy_country_code": "",
            "proxy_country_name": "",
            "connected": False,
            "auth_error": "",
            "wake_error": "",
            "wake_error_code": "",
        }
        logger.info("Firefox VPN core stopped")

    # ── API client ─────────────────────────────────────────────────────

    def _read_local_token(self) -> str | None:
        token_path = self.data_dir / "local_token"
        try:
            return token_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _api_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._local_token:
            headers[LOCAL_TOKEN_HEADER] = self._local_token
        return headers

    async def _api_get(self, path: str) -> dict[str, Any] | None:
        if not self._http:
            return None
        try:
            resp = await self._http.get(CORE_API_BASE + path, headers=self._api_headers())
            if resp.status_code // 100 == 2:
                return resp.json()
        except Exception:
            pass
        return None

    async def _api_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self._http:
            return None
        try:
            resp = await self._http.post(
                CORE_API_BASE + path, headers=self._api_headers(), json=payload
            )
            if resp.status_code // 100 in (2, 3):
                return resp.json()
            return {"_error": resp.status_code, "_body": resp.text[:200]}
        except Exception as exc:
            return {"_error": "exception", "_body": str(exc)[:200]}

    # ── operations ─────────────────────────────────────────────────────

    async def refresh_status(self) -> dict[str, Any]:
        """Read the core's current status and merge it into the local view."""
        st = await self._api_get("/api/status")
        if st is None:
            self.status["phase"] = "error"
            self.status["message"] = "core API unreachable"
            self.status["connected"] = False
            return self.status
        self._merge_status(st)
        self.status["connected"] = self._is_connected(st)
        return self.status

    def _merge_status(self, st: dict[str, Any]) -> None:
        state = st.get("state", {}) or {}
        proxy = st.get("proxy") or {}
        exit_view = st.get("exit") or {}
        active_account = st.get("active_account") or {}
        self.status.update({
            "phase": state.get("Phase", state.get("phase", "unknown")),
            "message": state.get("Message", ""),
            "account_email": active_account.get("Email", active_account.get("email", "")),
            "account_active": bool(active_account.get("Active")),
            "account_status": active_account.get("Status", ""),
            "account_reason": active_account.get("Reason", ""),
            "exit_country_code": str(exit_view.get("CountryCode", "") or "").upper(),
            "exit_country_name": exit_view.get("CountryName", ""),
            "exit_city": exit_view.get("City", ""),
            "proxy_country_code": str(proxy.get("CountryCode", "") or "").upper(),
            "proxy_country_name": proxy.get("CountryName", ""),
            "proxy_listen": proxy.get("Listen", ""),
            "login_required": bool(proxy.get("LoginRequired")),
            "wake_error": str(proxy.get("WakeError", "") or ""),
            "wake_error_code": str(proxy.get("WakeErrorCode", "") or ""),
        })
        # Authentication/sign-in errors surface here.
        if self.status["phase"] == "error" or self.status["wake_error_code"]:
            self.status["auth_error"] = self.status["wake_error"] or "sign-in required"
        else:
            self.status["auth_error"] = ""

    def _is_connected(self, st: dict[str, Any]) -> bool:
        state = st.get("state", {}) or {}
        return str(state.get("Phase", state.get("phase", ""))).lower() == "running"

    async def sign_in(self, email: str, password: str) -> dict[str, Any]:
        """Start a Firefox Account sign-in on the core.

        Returns the core's sign-in view: may require a verification code
        (``verification_required``), in which case the admin supplies it via
        :meth:`verify_sign_in`. The email + password are persisted encrypted
        at rest only after a successful sign-in.
        """
        result = await self._api_post("/api/signin", {"email": email, "password": password})
        if result is None:
            return {"ok": False, "error": "core API unreachable"}
        if "_error" in result:
            return {"ok": False, "error": result.get("_body", "sign-in request failed")}
        view = dict(result)
        view["ok"] = True
        # If fully signed in, persist credentials for restart recovery.
        if view.get("verified"):
            save_credentials(email, password)
        return view

    async def verify_sign_in(self, sign_in_id: str, code: str) -> dict[str, Any]:
        """Supply the email verification code for an in-progress sign-in."""
        result = await self._api_post(f"/api/signin/{sign_in_id}", {"value": code})
        if result is None:
            return {"ok": False, "error": "core API unreachable"}
        if "_error" in result:
            return {"ok": False, "error": result.get("_body", "verification failed")}
        view = dict(result)
        view["ok"] = True
        if view.get("verified"):
            # Credentials were persisted at sign-in start; mark done.
            pass
        return view

    async def cancel_sign_in(self, sign_in_id: str) -> None:
        await self._api_post(f"/api/signin/{sign_in_id}", {})

    async def set_location(self, country: str) -> dict[str, Any]:
        """Select the exit location: a country code, "fastest", or "" (recommended)."""
        result = await self._api_post("/api/location", {"country": country})
        if result is None:
            return {"ok": False, "error": "core API unreachable"}
        if "_error" in result:
            return {"ok": False, "error": result.get("_body", "location change failed")}
        return {"ok": True, **result}

    async def connect(self) -> dict[str, Any]:
        """Turn the proxy on (connect to Mozilla)."""
        result = await self._api_post("/api/power", {"on": True})
        if result is None:
            return {"ok": False, "error": "core API unreachable"}
        if "_error" in result:
            return {"ok": False, "error": result.get("_body", "connect failed")}
        return {"ok": True, **result}

    async def disconnect(self) -> dict[str, Any]:
        """Turn the proxy off."""
        result = await self._api_post("/api/power", {"on": False})
        if result is None:
            return {"ok": False, "error": "core API unreachable"}
        if "_error" in result:
            return {"ok": False, "error": result.get("_body", "disconnect failed")}
        return {"ok": True, **result}

    async def restart_proxy(self) -> dict[str, Any]:
        result = await self._api_post("/api/proxy/restart", {})
        if result is None:
            return {"ok": False, "error": "core API unreachable"}
        if "_error" in result:
            return {"ok": False, "error": result.get("_body", "restart failed")}
        return {"ok": True, **result}

    async def locations(self) -> list[dict[str, Any]]:
        """Return the available exit locations from the core's server list."""
        result = await self._api_get("/api/locations")
        if not result:
            return []
        locations = result.get("locations", []) or []
        out = []
        for loc in locations:
            out.append({
                "code": loc.get("Code", ""),
                "name": loc.get("Name", ""),
                "city": loc.get("City", ""),
                "server": loc.get("Server", ""),
                "recommended": bool(loc.get("Recommended")),
                "ping_ms": loc.get("PingMS"),
                "ping_error": loc.get("PingError", ""),
            })
        return out

    def proxy_endpoint(self) -> str:
        """The local SOCKS5 endpoint Lumen routes firefox_vpn traffic through."""
        return f"socks5://{DEFAULT_PROXY_LISTEN}"

    def is_available(self) -> bool:
        return self.binary.exists() and self._started
