"""Fail-closed API authentication, origin, and live-trading policy helpers."""

from __future__ import annotations

import hmac
import os
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

API_TOKEN_ENV = "FUNDING_ARB_API_TOKEN"
LIVE_TRADING_ENV = "FUNDING_ARB_ENABLE_LIVE_TRADING"
ALLOWED_ORIGINS_ENV = "FUNDING_ARB_ALLOWED_ORIGINS"

# Tokens are provisioned by the operator, not generated or persisted by the app.
# Restrict the alphabet to portable header/subprotocol characters and require a
# minimum length so an accidental short password does not enable the control API.
_API_TOKEN_RE = re.compile(r"[A-Za-z0-9._~-]{32,}\Z")
_WS_AUTH_PREFIX = "funding-arb-token."
WS_EVENTS_SUBPROTOCOL = "funding-arb-events.v1"

DEFAULT_ALLOWED_ORIGINS = frozenset(
    {
        "http://localhost:8787",
        "http://127.0.0.1:8787",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:1420",
        "http://127.0.0.1:1420",
        "tauri://localhost",
        "http://tauri.localhost",
    }
)

# Only scanner result/status reads are public. Scan triggers and every other /api
# control route require a configured bearer token.
_PUBLIC_SCANNER_ROUTES = frozenset(
    {
        ("GET", "/api/scanner/status"),
        ("GET", "/api/scanner/opportunities"),
    }
)


def _environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def configured_api_token(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return a valid configured token; missing/weak values disable control APIs."""
    token = _environment(environ).get(API_TOKEN_ENV, "")
    if not _API_TOKEN_RE.fullmatch(token):
        return None
    return token


def is_api_authorized(
    authorization: str | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Validate an HTTP Bearer token without revealing the configured value."""
    expected = configured_api_token(environ)
    if expected is None or not authorization:
        return False
    scheme, separator, presented = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not presented:
        return False
    if " " in presented or "\t" in presented:
        return False
    return hmac.compare_digest(presented, expected)


def is_websocket_authorized(
    authorization: str | None,
    subprotocol_header: str | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Authenticate a WS using Bearer auth or a private auth subprotocol.

    Browser WebSocket APIs cannot set Authorization. Browser clients should offer
    both ``funding-arb-events.v1`` and ``funding-arb-token.<token>`` as protocols;
    the server authenticates the latter and negotiates only the non-secret events
    protocol. Native clients may use a normal Authorization header.
    """
    if is_api_authorized(authorization, environ=environ):
        return True
    expected = configured_api_token(environ)
    if expected is None or not subprotocol_header:
        return False
    for item in subprotocol_header.split(","):
        protocol = item.strip()
        if protocol.startswith(_WS_AUTH_PREFIX):
            presented = protocol[len(_WS_AUTH_PREFIX) :]
            if hmac.compare_digest(presented, expected):
                return True
    return False


def websocket_events_subprotocol(subprotocol_header: str | None) -> str | None:
    """Return the public event protocol if offered by a WS client."""
    if not subprotocol_header:
        return None
    offered = {item.strip() for item in subprotocol_header.split(",")}
    if WS_EVENTS_SUBPROTOCOL in offered:
        return WS_EVENTS_SUBPROTOCOL
    return None


def request_requires_auth(path: str, method: str) -> bool:
    """Require auth by default for API routes, with a narrow public scanner set."""
    normalized_method = method.upper()
    if normalized_method == "OPTIONS":
        return False
    if path == "/api" or path.startswith("/api/"):
        return (normalized_method, path) not in _PUBLIC_SCANNER_ROUTES
    return False


def _valid_origin_value(origin: str) -> bool:
    if not origin or origin in {"null", "*"}:
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"http", "https", "tauri", "app"}
        and parsed.netloc
        and not parsed.username
        and not parsed.password
        and parsed.path == ""
        and not parsed.query
        and not parsed.fragment
    )


def allowed_browser_origins(
    environ: Mapping[str, str] | None = None,
) -> frozenset[str]:
    """Return exact trusted UI origins, with optional explicit operator additions."""
    origins = set(DEFAULT_ALLOWED_ORIGINS)
    configured = _environment(environ).get(ALLOWED_ORIGINS_ENV, "")
    for candidate in configured.split(","):
        origin = candidate.strip()
        if _valid_origin_value(origin):
            origins.add(origin)
    return frozenset(origins)


def is_origin_allowed(
    origin: str | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Allow originless native clients; browser-supplied origins must match exactly."""
    if origin is None:
        return True
    return origin in allowed_browser_origins(environ)


def live_trading_enabled(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Live routes stay disabled unless the operator sets the exact value ``1``."""
    return _environment(environ).get(LIVE_TRADING_ENV) == "1"
