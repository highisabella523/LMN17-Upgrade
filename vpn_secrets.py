"""Encrypted at-rest storage for the Firefox VPN upstream account credentials.

The Firefox VPN integration authenticates to Mozilla with a Firefox Account
email and password. Those credentials must never appear in source control,
in Docker image layers, in API responses, or in logs. This module persists
them in a small sidecar file next to Lumen's state, encrypted with a key
derived from Lumen's existing ``SECRET_KEY`` (which itself is persisted in
``code_secret.key`` and survives restarts).

Two backends are supported:
  * ``cryptography`` (Fernet / AES-128-CBC with HMAC) when the package is
    installed — real authenticated encryption.
  * A stdlib-only fallback: PBKDF2-derived key, AES is unavailable in the
    standard library, so the fallback uses a keyed stream construction built
    from ``hashlib.pbkdf2_hmac`` over SHA-256. This is obfuscation-grade at
    rest; it keeps the password out of casual inspection and out of logs/API
    responses, and the sidecar file is mode 0600.

Either way the upstream password is never hard-coded, never transmitted to
the end user, and only ever sent to Mozilla's own authentication endpoints.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

log = __import__("logging").getLogger("Lumen.vpn_secrets")

SIDEcar_NAME = "firefox_vpn_secret.enc"
# Sidecar file is owner-only: the relay process runs as a dedicated, unprivileged
# user on Railway so no other process in the container can read it.
SIDEcar_MODE = 0o600

_HAS_CRYPTOGRAPHY = False
try:  # pragma: no cover - environment dependent
    from cryptography.fernet import Fernet  # type: ignore

    _HAS_CRYPTOGRAPHY = True
except Exception:  # cryptography not installed; use the stdlib fallback
    Fernet = None  # type: ignore


def _secret_key() -> bytes:
    """Lumen's process secret, persisted in code_secret.key or SECRET_KEY env."""
    import main  # imported here to avoid any import-order surprises

    secret = main.CONFIG.get("secret") if hasattr(main, "CONFIG") else ""
    if not secret:
        secret = os.environ.get("SECRET_KEY", "")
    return secret.encode("utf-8")


def _derive_key(salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", _secret_key(), salt, 200_000, dklen=32)


class Vault:
    """Encrypt/decrypt a small JSON payload at rest."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (Path(os.environ.get("DATA_DIR", "/data")) / SIDEcar_NAME)

    # ── cryptography backend ──────────────────────────────────────────────

    def _fernet(self) -> "Fernet":
        key = base64.urlsafe_b64encode(_derive_key(b"lumen-vpn-fernet"))
        return Fernet(key)

    def _encrypt_fernet(self, plaintext: bytes) -> bytes:
        return self._fernet().encrypt(plaintext)

    def _decrypt_fernet(self, blob: bytes) -> bytes:
        return self._fernet().decrypt(blob)

    # ── stdlib fallback backend ───────────────────────────────────────────

    def _encrypt_fallback(self, plaintext: bytes) -> bytes:
        salt = os.urandom(16)
        key = _derive_key(salt)
        out = bytearray()
        ki = 0
        block = 0
        # Stream XOR with a keyed, counter-fed PBKDF2 stream. Re-keyed every
        # 64 bytes so a reused stream segment never repeats within a record.
        for i, b in enumerate(plaintext):
            if i % 64 == 0:
                block += 1
                keystream = hashlib.pbkdf2_hmac(
                    "sha256", key, salt + block.to_bytes(4, "big"), 1, dklen=64
                )
                ki = 0
            out.append(b ^ keystream[ki])
            ki += 1
        return b"FB1" + salt + bytes(out)

    def _decrypt_fallback(self, blob: bytes) -> bytes:
        if not blob.startswith(b"FB1"):
            raise ValueError("unknown vault format")
        salt = blob[3:19]
        key = _derive_key(salt)
        ciphertext = blob[19:]
        out = bytearray()
        ki = 0
        block = 0
        for i, b in enumerate(ciphertext):
            if i % 64 == 0:
                block += 1
                keystream = hashlib.pbkdf2_hmac(
                    "sha256", key, salt + block.to_bytes(4, "big"), 1, dklen=64
                )
                ki = 0
            out.append(b ^ keystream[ki])
            ki += 1
        return bytes(out)

    # ── public API ────────────────────────────────────────────────────────

    def write(self, credentials: dict) -> None:
        """Persist credentials (email, password) encrypted at rest."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        plaintext = json.dumps(credentials, ensure_ascii=False).encode("utf-8")
        if _HAS_CRYPTOGRAPHY:
            blob = self._encrypt_fernet(plaintext)
        else:
            blob = self._encrypt_fallback(plaintext)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, SIDEcar_MODE)
        except OSError:
            pass
        log.info("Firefox VPN credentials saved to %s (mode %o)", self.path, SIDEcar_MODE)

    def read(self) -> dict | None:
        """Return the stored credentials, or None if nothing is stored."""
        if not self.path.exists():
            return None
        blob = self.path.read_bytes()
        try:
            if _HAS_CRYPTOGRAPHY:
                plaintext = self._decrypt_fernet(blob)
            else:
                plaintext = self._decrypt_fallback(blob)
        except Exception as exc:  # wrong key or corrupted file
            log.warning("Could not decrypt Firefox VPN credentials: %s", exc)
            return None
        try:
            return json.loads(plaintext.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def stored_credentials() -> dict | None:
    """Convenience: read the persisted Firefox VPN credentials."""
    return Vault().read()


def save_credentials(email: str, password: str) -> None:
    """Convenience: persist the Firefox VPN credentials encrypted at rest."""
    Vault().write({"email": email, "password": password})


def clear_credentials() -> None:
    Vault().clear()
