"""Mode B: private single-user HTTP server.

Serves the same three tools over streamable HTTP so you can reach them from a
phone, plus one extra endpoint that accepts a pushed portal session from your
laptop. It holds exactly one session — yours. It never asks a caller to
authenticate to AVEVA and has no code path that accepts a caller's AVEVA
credentials.

Every request must carry the shared secret in PI_KB_MCP_SECRET. Anyone who has
that secret spends your support entitlement under your identity, so treat it
like a password and never publish this URL.
"""

from __future__ import annotations

import hmac
import logging
import os
import sys

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .server import mcp
from .session_store import relevant, save_cookies

log = logging.getLogger(__name__)

SECRET_ENV = "PI_KB_MCP_SECRET"
MIN_SECRET_LEN = 24


def _secret() -> str:
    secret = os.environ.get(SECRET_ENV, "")
    if len(secret) < MIN_SECRET_LEN:
        sys.exit(
            f"{SECRET_ENV} must be set to a random string of at least "
            f"{MIN_SECRET_LEN} characters before this server will start.\n"
            "Generate one with:  python -c 'import secrets; print(secrets.token_urlsafe(32))'"
        )
    return secret


def _presented(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


class RequireSecret(BaseHTTPMiddleware):
    """Reject anything without the shared secret, before it reaches a tool."""

    def __init__(self, app, secret: str) -> None:
        super().__init__(app)
        self._secret = secret

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        # compare_digest to keep the check constant-time.
        if not hmac.compare_digest(_presented(request), self._secret):
            log.warning("rejected unauthenticated request to %s", request.url.path)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def health(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def push_session(request: Request) -> Response:
    """Accept portal cookies pushed by `pi-kb-mcp login --push`.

    Deliberately logs nothing about the payload beyond how many cookies were
    kept — these values are credentials.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "expected JSON"}, status_code=400)

    cookies = payload.get("cookies") if isinstance(payload, dict) else None
    if not isinstance(cookies, list) or not cookies:
        return JSONResponse({"error": "expected a non-empty 'cookies' list"}, status_code=400)

    kept = relevant(cookies)
    if not kept:
        return JSONResponse(
            {"error": "no portal session cookies found in the payload"}, status_code=400
        )

    save_cookies(kept)
    log.info("stored a pushed portal session (%d cookies)", len(kept))
    return JSONResponse({"status": "stored", "cookies": len(kept)})


def build_app() -> Starlette:
    """Extend the MCP app rather than mounting it.

    Mounting drops the session manager's lifespan, which the streamable-HTTP
    transport needs, so the extra routes are added to the MCP app itself.
    """
    secret = _secret()
    app = mcp.streamable_http_app()
    app.add_route("/health", health, methods=["GET"])
    app.add_route("/session", push_session, methods=["POST"])
    app.add_middleware(RequireSecret, secret=secret)
    return app


def run() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(build_app(), host="0.0.0.0", port=port)
