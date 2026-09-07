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
from starlette.responses import HTMLResponse, JSONResponse, Response

from mcp.server.transport_security import TransportSecuritySettings

from .server import mcp
from .session_store import SESSION_COOKIE, relevant, save_cookies

log = logging.getLogger(__name__)

SECRET_ENV = "PI_KB_MCP_SECRET"
HOSTS_ENV = "PI_KB_MCP_ALLOWED_HOSTS"
MIN_SECRET_LEN = 24

UNAUTHENTICATED = {"/health"}


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
        if request.url.path in UNAUTHENTICATED:
            return await call_next(request)
        # The sign-in form itself is static HTML holding no secrets, and a phone
        # browser cannot set an Authorization header on a plain navigation. The
        # POST that submits it carries the secret and is checked like everything
        # else, so opening the page grants nothing.
        if request.url.path == "/login" and request.method == "GET":
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


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in \u2014 pi-kb-mcp</title>
<style>
  :root { color-scheme: light dark; --fg:#111; --bg:#fff; --mut:#666; --line:#ccc; --err:#b00020; --ok:#0a6; }
  @media (prefers-color-scheme: dark) {
    :root { --fg:#eee; --bg:#151515; --mut:#aaa; --line:#444; --err:#ff6b7f; --ok:#4ade80; }
  }
  body { margin:0; padding:1.5rem; font:16px/1.5 system-ui,-apple-system,sans-serif;
         color:var(--fg); background:var(--bg); }
  main { max-width:26rem; margin:0 auto; }
  h1 { font-size:1.25rem; margin:0 0 .25rem; }
  p.sub { color:var(--mut); margin:0 0 1.5rem; font-size:.875rem; }
  label { display:block; margin:1rem 0 .25rem; font-weight:600; font-size:.875rem; }
  input { width:100%; box-sizing:border-box; padding:.75rem; font-size:16px;
          border:1px solid var(--line); border-radius:.5rem;
          background:var(--bg); color:var(--fg); }
  button { width:100%; margin-top:1.5rem; padding:.85rem; font-size:1rem; font-weight:600;
           border:0; border-radius:.5rem; background:var(--fg); color:var(--bg); }
  button[disabled] { opacity:.5; }
  #msg { margin-top:1rem; font-size:.875rem; white-space:pre-wrap; word-wrap:break-word; }
  .err { color:var(--err); } .ok { color:var(--ok); }
  footer { margin-top:2rem; color:var(--mut); font-size:.75rem; }
</style></head><body><main>
<h1>Refresh AVEVA session</h1>
<p class="sub">Signs this server back in to the AVEVA support portal.</p>
<!-- Two separate <form>s, deliberately: the secret is a standing credential
     you already use on every MCP request, and should behave like any other
     saved password. The AVEVA fields are the opposite -- never remembered,
     matching the "used once and dropped" promise below -- so they sit in
     their own autocomplete="off" form the browser cannot fold in with the
     first one. Submission is handled in JS either way; the <form> tags exist
     only to give the browser's password manager an unambiguous boundary. -->
<form id="secretForm" autocomplete="on">
  <label for="s">Server secret</label>
  <input id="s" name="secret" type="password" autocomplete="current-password" required>
</form>
<form id="avevaForm" autocomplete="off">
  <label for="u">AVEVA username</label>
  <input id="u" name="username" type="text" autocomplete="off" autocapitalize="none"
         autocorrect="off" spellcheck="false" required>
  <label for="p">AVEVA password</label>
  <input id="p" name="password" type="password" autocomplete="off" required>
</form>
<button id="b" type="button">Sign in</button>
<div id="msg"></div>
<footer>Your server secret is your browser's to remember, like any saved password.
Your AVEVA username and password are not: they are used once to obtain a session
cookie and are never written to disk, logged, or saved by this page or your
browser.</footer>
</main><script>
const b=document.getElementById('b'), m=document.getElementById('msg');
const s=document.getElementById('s'), u=document.getElementById('u'), p=document.getElementById('p');

async function submit() {
  b.disabled = true; m.className=''; m.textContent = 'Signing in\u2026 this takes up to a minute.';
  try {
    const r = await fetch('/login', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + s.value, 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: u.value, password: p.value })
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok) {
      // A JS-driven fetch() login gives most browsers no signal to offer
      // saving a password -- there is no real <form> submit event for them to
      // key off. navigator.credentials.store() is the explicit, supported
      // replacement for exactly this case (see web.dev's sign-in-form-best
      // -practices guidance); without it this prompt was never going to
      // appear, on this page or any other JS-driven login. Best-effort: some
      // browsers (notably Safari/iOS) do not implement it, so failures here
      // are silently ignored rather than surfaced as an error.
      if (window.PasswordCredential && navigator.credentials && navigator.credentials.store) {
        try {
          await navigator.credentials.store(
            new PasswordCredential({ id: 'secret', name: 'pi-kb-mcp server secret', password: s.value })
          );
        } catch (e) { /* unsupported or declined -- not fatal */ }
      }
      m.className='ok';
      m.textContent = 'Signed in. Stored ' + d.cookies + ' session cookies. You can close this page.';
      u.value = ''; p.value = '';   // the secret is left in place on purpose
    } else if (r.status === 401) {
      m.className='err'; m.textContent = 'Wrong server secret.';
    } else {
      m.className='err'; m.textContent = d.error || ('Failed (HTTP ' + r.status + ').');
    }
  } catch (err) {
    m.className='err'; m.textContent = 'Could not reach the server.';
  }
  b.disabled = false;
}

b.addEventListener('click', submit);
for (const field of [s, u, p]) {
  field.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
}
</script></body></html>
"""


async def login_page(request: Request) -> Response:
    """The form itself. Static, secret-free, and safe to serve unauthenticated."""
    return HTMLResponse(LOGIN_PAGE)


async def sign_in(request: Request) -> Response:
    """Sign in to AVEVA with credentials supplied from the phone.

    The payload is read, used once, and dropped: never logged, never written to
    disk, never echoed back \u2014 including in error responses, which describe the
    page AVEVA returned rather than anything that was typed.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "expected JSON"}, status_code=400)

    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)

    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password:
        return JSONResponse(
            {"error": "username and password are both required"}, status_code=400
        )

    from .portal_login import LoginError, sign_in as portal_sign_in

    try:
        cookies = await portal_sign_in(username, password)
    except LoginError as exc:
        # str(exc) describes the page reached, never the credentials.
        log.warning("portal sign-in did not complete")
        return JSONResponse({"error": str(exc)}, status_code=502)
    finally:
        # Best-effort: drops this function's references. Python cannot guarantee
        # the bytes leave memory — see the note in portal_login.
        password = payload = None

    # Belt and braces: sign_in only returns on success, but a jar without the
    # session cookie must never replace a working one.
    if not any(c.get("name") == SESSION_COOKIE for c in cookies):
        return JSONResponse(
            {"error": "sign-in returned no session cookie; kept the existing session"},
            status_code=502,
        )

    save_cookies(cookies)
    log.info("stored a session from an interactive sign-in (%d cookies)", len(cookies))
    return JSONResponse({"status": "stored", "cookies": len(cookies)})


def _transport_security() -> TransportSecuritySettings:
    """Decide how the MCP transport treats the Host header.

    The SDK's DNS-rebinding protection trusts only localhost by default, so a
    server published under a real hostname answers 421 "Invalid Host header" to
    every MCP request.

    Set PI_KB_MCP_ALLOWED_HOSTS (comma-separated) to keep that protection and
    name the hostnames you serve. With it unset, the protection is switched off:
    it defends browser-driven rebinding against an unauthenticated local server,
    and every request here must already carry the shared secret, which such an
    attacker cannot obtain.
    """
    hosts = [h.strip() for h in os.environ.get(HOSTS_ENV, "").split(",") if h.strip()]
    if hosts:
        log.info("restricting MCP transport to hosts: %s", ", ".join(hosts))
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=[f"https://{h}" for h in hosts],
        )
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


def build_app() -> Starlette:
    """Extend the MCP app rather than mounting it.

    Mounting drops the session manager's lifespan, which the streamable-HTTP
    transport needs, so the extra routes are added to the MCP app itself.
    """
    secret = _secret()
    app = mcp.streamable_http_app(transport_security=_transport_security())
    app.add_route("/health", health, methods=["GET"])
    app.add_route("/session", push_session, methods=["POST"])
    app.add_route("/login", login_page, methods=["GET"])
    app.add_route("/login", sign_in, methods=["POST"])
    app.add_middleware(RequireSecret, secret=secret)
    return app


def run() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(build_app(), host="0.0.0.0", port=port)
