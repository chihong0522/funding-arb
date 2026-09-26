#!/usr/bin/env python3
"""Unified credential provider — automatically selects the most secure backend available for the current platform.

Credential sources (highest security first):
  1. keyring       — macOS Keychain / Windows Credential Manager / Linux Secret Service
  2. systemd-creds — Linux machine-bound (TPM2 / machine-id), recommended for headless servers
  3. age           — encrypted files, protects against accidental exposure but not malicious same-user processes

Plaintext credentials.json files are never loaded.

Usage (in venue modules):
  from core.credentials import ensure_env
  ensure_env()           # Load all credentials once
  ensure_env("BITGET_")  # Only load BITGET_* series

After that, simply use os.environ["BITGET_API_KEY"] as usual.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Credential store location: ~/.funding-arb (keyring service "funding-arb").
_APP_DIR = Path.home() / ".funding-arb"
_AGE_DIRS = [_APP_DIR]
_SYSTEMD_CREDS_DIRS = [Path("/etc/funding-arb/creds")]
_SERVICES = ["funding-arb"]

_KNOWN_PREFIXES = (
    "BINANCE_",
    "BITGET_",
    "BYBIT_",
    "OKX_",
    "HYPERLIQUID_",
    "EDGEX_",
    "ASTER_",
    "DYDX_",
    "LIGHTER_",
    "TELEGRAM_",
    "TRADE_SIGNER_",
)

_ALL_KEYS = [
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BINANCE_TRADE_API_KEY",
    "BINANCE_SECRET_KEY",
    "BINANCE_TRADE_SECRET_KEY",
    "BITGET_API_KEY",
    "BITGET_SECRET_KEY",
    "BITGET_PASSPHRASE",
    "BYBIT_API_KEY",
    "BYBIT_SECRET_KEY",
    "OKX_API_KEY",
    "OKX_SECRET_KEY",
    "OKX_PASSPHRASE",
    "HYPERLIQUID_API_KEY",
    "HYPERLIQUID_API_SECRET",
    "EDGEX_ACCOUNT_ID",
    "EDGEX_TRADING_PRIVATE_KEY",
    "ASTER_API_KEY",
    "ASTER_API_SECRET",
    "DYDX_MNEMONIC",
    "LIGHTER_API_PRIVATE_KEY",
    "HYPERLIQUID_PRIVATE_KEY",
    "TRADE_SIGNER_API_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]

_loaded = False
_cache: dict[str, str] | None = None


# Backend 1: keyring (macOS Keychain / Windows Credential Manager / Linux Secret Service)
def _load_keyring() -> dict[str, str] | None:
    """Read from system keychain. Returns None if unavailable, empty dict if available but no data."""
    try:
        import keyring
    except ImportError:
        return None

    result: dict[str, str] = {}
    try:
        for k in _ALL_KEYS:
            v = keyring.get_password(_SERVICES[0], k)
            if v:
                result[k] = v
    except Exception:
        return None
    return result


# Backend 2: systemd-creds (Linux machine-bound, TPM2 / machine-id)
def _load_systemd_creds() -> dict[str, str]:
    """Read from systemd-creds encrypted files (Linux only)."""
    if sys.platform != "linux":
        return {}

    sd_creds = shutil.which("systemd-creds")
    if not sd_creds:
        return {}

    result: dict[str, str] = {}
    for creds_dir in _SYSTEMD_CREDS_DIRS:
        for key_name in _ALL_KEYS:
            if key_name in result:
                continue
            cred_file = creds_dir / f"{key_name}.cred"
            if not cred_file.exists():
                continue
            try:
                proc = subprocess.run(
                    [sd_creds, "decrypt", str(cred_file)],
                    capture_output=True,
                    timeout=5,
                )
                if proc.returncode == 0:
                    result[key_name] = proc.stdout.decode().strip()
            except (subprocess.TimeoutExpired, OSError):
                pass

    return result


# Backend 3: age encrypted files
def _load_age() -> dict[str, str]:
    """Read from age encrypted files."""
    age = shutil.which("age")
    if not age:
        return {}

    for d in _AGE_DIRS:
        identity_file = d / "credentials.key"
        encrypted_file = d / "credentials.enc"
        if not encrypted_file.exists() or not identity_file.exists():
            continue
        try:
            result = subprocess.run(
                [age, "-d", "-i", str(identity_file), str(encrypted_file)],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                return json.loads(result.stdout.decode())
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            pass

    return {}


# Unified loading (merges secure backends, higher-priority sources overwrite lower)
def _load_all() -> dict[str, str]:
    global _cache
    if _cache is not None:
        return _cache

    merged: dict[str, str] = {}

    # Medium: age encrypted
    merged.update(_load_age())

    # High: systemd-creds (Linux)
    merged.update(_load_systemd_creds())

    # Highest: keyring (macOS / Windows / Linux desktop)
    kr = _load_keyring()
    if kr is not None:
        merged.update(kr)

    # Only keep known keys
    merged = {k: v for k, v in merged.items() if _is_known_key(k)}

    _cache = merged
    return _cache


def _is_known_key(key: str) -> bool:
    return any(key.startswith(p) for p in _KNOWN_PREFIXES)


# Public API
def ensure_env(prefix: str = "") -> None:
    """Load credentials into os.environ.

    Values already present in os.environ are not overwritten (env vars always have highest priority).
    """
    global _loaded
    if _loaded and not prefix:
        return

    all_creds = _load_all()

    for key, value in all_creds.items():
        if prefix and not key.startswith(prefix):
            continue
        if not os.environ.get(key):
            os.environ[key] = value

    if not prefix:
        _loaded = True


def get_credential(key: str) -> str | None:
    """Retrieve a single credential directly without writing to os.environ."""
    val = os.environ.get(key)
    if val:
        return val
    return _load_all().get(key)


def redact_secret_values(value: object) -> str:
    """Redact configured credential values and common signed-request fields."""
    text = str(value)
    for key in _ALL_KEYS:
        secret = os.environ.get(key)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return re.sub(
        r"(?i)(signature|api[_-]?key|secret|token|passphrase|mnemonic|private[_-]?key)([=: ]+)[^&\s,}'\"]+",
        r"\1\2[REDACTED]",
        text,
    )
