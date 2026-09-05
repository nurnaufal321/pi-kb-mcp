"""Session handling for the AVEVA Customer Support Portal.

This module deliberately does NOT touch browser cookie stores, keychains, or any
other credential store. It reads a bearer token that `pi-kb-mcp login` captured
earlier and wrote to disk. Acquiring the token requires a browser and lives in
login.py, which the server process never imports.
"""

import json
import os
import time
from pathlib import Path

SESSION_PATH = Path(
    os.environ.get("PI_KB_MCP_SESSION")
    or Path.home() / ".config" / "pi-kb-mcp" / "session.json"
)

LOGIN_HINT = (
    "No AVEVA portal session found. Run `pi-kb-mcp login` and sign in, "
    "then retry. (Alternatively set AVEVA_KB_TOKEN to a bearer token.)"
)
EXPIRED_HINT = (
    "AVEVA portal session expired. Run `pi-kb-mcp login` and sign in again, "
    "then retry."
)


class AuthError(RuntimeError):
    """Raised when no usable session is available. Message is user-facing."""


def save_token(token: str, expires_at: int | None = None) -> Path:
    """Persist a bearer token to the session file with 0600 permissions."""
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"token": token, "expires_at": expires_at}
    # Create with restrictive permissions before writing any secret material.
    fd = os.open(SESSION_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)
    return SESSION_PATH


def token_expiry(token: str) -> int | None:
    """Read `exp` out of a JWT without verifying it. None if unreadable."""
    import base64

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return None


def get_token() -> str:
    """Resolve a bearer token, freshest source first.

    Resolved per call rather than cached at startup so that a re-login is picked
    up without restarting the server, and so no token outlives a single request.
    """
    env = os.environ.get("AVEVA_KB_TOKEN", "").strip()
    if env:
        return env

    try:
        data = json.loads(SESSION_PATH.read_text())
    except FileNotFoundError:
        raise AuthError(LOGIN_HINT) from None
    except (OSError, ValueError):
        raise AuthError(LOGIN_HINT) from None

    token = (data.get("token") or "").strip()
    if not token:
        raise AuthError(LOGIN_HINT)

    expires_at = data.get("expires_at") or token_expiry(token)
    # 60s of slack so a token doesn't expire mid-flight.
    if expires_at and time.time() > expires_at - 60:
        raise AuthError(EXPIRED_HINT)

    return token


async def get_token_async() -> str:
    """Like get_token, but in Mode B mint a new token instead of giving up.

    The stdio server has no way to refresh unattended and must ask the user to
    log in; the HTTP server holds portal cookies and can boot the SPA headlessly.
    """
    try:
        return get_token()
    except AuthError:
        if not _refresh_enabled():
            raise
        from .refresh import RefreshError, mint_token
        try:
            token = await mint_token()
        except RefreshError as exc:
            raise AuthError(str(exc)) from None
        save_token(token, token_expiry(token))
        return token


def _refresh_enabled() -> bool:
    """Only Mode B refreshes itself; the flag is set by the HTTP entry point."""
    return os.environ.get("PI_KB_MCP_SELF_REFRESH") == "1"


async def auth_headers_async() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {await get_token_async()}",
        "Accept": "application/json, text/plain, */*",
    }


def auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_token()}",
        "Accept": "application/json, text/plain, */*",
    }
