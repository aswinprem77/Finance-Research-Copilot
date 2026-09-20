"""
API key authentication for the read API.

The API serves a watchlist's filing analysis and, through it, whatever the
memos contain. On localhost that is the operator's own screen. On a public
URL it is whoever finds the hostname, so this exists before any deployment
rather than after one.

The rule that matters is the default, not the mechanism:

    no key configured + loopback bind  -> open, because that is the operator
                                          on their own machine
    no key configured + any other bind -> REFUSED at startup

Defaulting to open would mean a deploy silently exposes everything, and the
person doing it would have no signal. Defaulting to closed everywhere would
break the local flow the Quickstart describes and push people toward
disabling auth entirely, which is worse. Refusing to bind publicly without a
key puts the decision exactly where it belongs: you cannot expose this
without saying so.

Keys are compared with secrets.compare_digest, so a wrong key takes the same
time as a right one and cannot be discovered a character at a time. Several
keys may be configured at once so one can be rotated out without downtime.

What this is NOT: it is a shared secret, not a user system. There are no
accounts, roles, or per-user audit. It does not rate-limit, and it does not
provide transport security - a key sent over plain HTTP is readable in
transit, so terminate TLS in front of this.
"""
from __future__ import annotations

import ipaddress
import os
import secrets
from collections.abc import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

ENV_API_KEYS = "COPILOT_API_KEYS"
HEADER_NAME = "X-API-Key"

# Reachable without a key so a load balancer or container probe does not need
# the credential. It reports liveness only - no paths, counts or filing data.
PUBLIC_PATHS = frozenset({"/livez"})


def load_api_keys(env: dict[str, str] | None = None) -> set[str]:
    """
    Configured keys, comma-separated. Blank entries are ignored so a trailing
    comma or an empty variable does not silently become a valid empty key.
    """
    env = os.environ if env is None else env
    raw = env.get(ENV_API_KEYS) or ""
    return {key.strip() for key in raw.split(",") if key.strip()}


def is_loopback(host: str) -> bool:
    """
    True when binding `host` exposes nothing beyond this machine.

    An empty host, "0.0.0.0" and "::" all mean every interface, so they are
    emphatically not loopback. A name that is not an IP literal is treated as
    non-loopback: it may resolve anywhere, and guessing wrong here fails open.
    """
    candidate = (host or "").strip()
    if not candidate:
        return False
    if candidate.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def presented_key(headers) -> str | None:
    """Accept either the dedicated header or a bearer token, since clients differ."""
    key = headers.get(HEADER_NAME)
    if key:
        return key.strip()
    authorization = headers.get("Authorization") or ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def key_is_valid(candidate: str | None, keys: Iterable[str]) -> bool:
    """
    Constant-time comparison against every configured key.

    Every key is checked even after a match so the number of comparisons does
    not depend on which key was presented.
    """
    if not candidate:
        return False
    matched = False
    for key in keys:
        if secrets.compare_digest(candidate, key):
            matched = True
    return matched


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """
    Checks every request except PUBLIC_PATHS.

    Middleware rather than a per-route dependency so that nothing is protected
    by remembering to decorate it - including /docs and /openapi.json, which
    describe the shape of everything else.
    """

    def __init__(self, app, keys: Iterable[str]):
        super().__init__(app)
        self._keys = {k for k in keys if k}

    async def dispatch(self, request, call_next):
        if not self._keys or request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        if key_is_valid(presented_key(request.headers), self._keys):
            return await call_next(request)
        # No hint about whether the key was absent, malformed or simply wrong.
        return JSONResponse(
            status_code=401,
            content={"detail": f"Missing or invalid API key. Send it as the {HEADER_NAME} "
                               "header or as an Authorization: Bearer token."},
            headers={"WWW-Authenticate": f'{HEADER_NAME} realm="financial-research-copilot"'},
        )


def describe(keys: Iterable[str], host: str) -> str:
    """One line for the operator at startup, so the security posture is never a guess."""
    count = len({k for k in keys if k})
    if count:
        return f"Auth: API key required ({count} key(s) configured)."
    return ("Auth: DISABLED - no COPILOT_API_KEYS set. "
            f"Serving on {host}, which is local only.")
