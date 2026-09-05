"""Interactive login: capture a portal bearer token using the OS web view.

Runs only when the user invokes `pi-kb-mcp login`. The MCP server never imports
this module, so the server process has no browser and no credential-store access.

How the token is obtained: the portal's SPA holds its JWT in memory only — it is
absent from cookies, localStorage and sessionStorage, and the token-issuer
endpoint cannot be called directly (see NOTES.md). So instead of replicating the
mint, we observe it: hook `fetch` in the loaded page, nudge the SPA into making
one ordinary API call, and read the Authorization header it sets.

The user's password is typed into AVEVA's own ADFS page inside the web view.
It is never seen, handled or stored by this code.
"""

import json
import time

from .auth import save_token, token_expiry

HOME = "https://softwaresupportsp.aveva.com/en-US/home"

# Hook fetch, then type into the portal's own search box, which fires
# POST /Search/QuerySuggestions carrying the Authorization header.
_CAPTURE_JS = r"""
(function () {
  if (window.__pikb) return 'already';
  window.__pikb = [];
  var of = window.fetch;
  window.fetch = function (i, init) {
    try {
      var h = init && init.headers;
      var a = h && (h.Authorization || h.authorization);
      if (a) window.__pikb.push(String(a));
    } catch (e) {}
    return of.apply(this, arguments);
  };
  return 'hooked';
})()
"""

_NUDGE_JS = r"""
(function () {
  var i = document.getElementById('basic-url')
       || document.querySelector('input[type=search]');
  if (!i) return 'no-search-box';
  var set = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, 'value').set;
  set.call(i, 'pi');
  i.dispatchEvent(new Event('input', { bubbles: true }));
  i.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'i' }));
  return 'nudged';
})()
"""

_READ_JS = "JSON.stringify(window.__pikb || [])"


def _extract(raw: str) -> str:
    token = raw.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _cookies_from_window(window) -> list[dict]:
    """Normalise pywebview's cookies into the shape Playwright expects."""
    try:
        raw = window.get_cookies() or []
    except Exception:
        return []

    out: list[dict] = []
    for entry in raw:
        # pywebview yields http.cookies.SimpleCookie objects.
        morsels = entry.values() if hasattr(entry, "values") else [entry]
        for morsel in morsels:
            name = getattr(morsel, "key", None)
            value = getattr(morsel, "value", None)
            if not name or value is None:
                continue
            out.append({
                "name": name,
                "value": value,
                "domain": morsel.get("domain") or "softwaresupportsp.aveva.com",
                "path": morsel.get("path") or "/",
                "httpOnly": bool(morsel.get("httponly")),
                "secure": True,
                "sameSite": "None",
            })
    return out


def push_session(url: str, secret: str, cookies: list[dict]) -> None:
    """Send the portal session to a private Mode B instance over TLS."""
    import httpx

    from .session_store import relevant

    kept = relevant(cookies)
    if not kept:
        raise SystemExit(
            "No portal session cookies were captured, so there is nothing to push. "
            "This can happen if the web view blocks cookie access; see the README."
        )

    endpoint = url.rstrip("/") + "/session"
    if not endpoint.startswith("https://") and "localhost" not in endpoint:
        raise SystemExit(
            f"Refusing to push a session over plain HTTP to {endpoint}. Use https."
        )

    resp = httpx.post(
        endpoint,
        json={"cookies": kept},
        headers={"Authorization": f"Bearer {secret}"},
        timeout=30.0,
    )
    if resp.status_code == 401:
        raise SystemExit("Server rejected the secret. Check PI_KB_MCP_SECRET matches.")
    resp.raise_for_status()
    print(f"Pushed {len(kept)} session cookies to {url}.")


def run_login(timeout: int = 600, push_url: str | None = None) -> str:
    try:
        import webview
    except ImportError:
        raise SystemExit(
            "pywebview is not installed. Install the login extra:\n"
            "  uv tool install 'pi-kb-mcp[login]'   (or: pip install 'pi-kb-mcp[login]')\n"
            "On Linux this also needs GTK/WebKit system packages; if that is not\n"
            "practical, set AVEVA_KB_TOKEN instead."
        ) from None

    captured: dict = {}

    def watch(window) -> None:
        print("Sign in to the AVEVA portal in the window that opened.")
        print("Tick 'Keep me signed in' — it lengthens the session.")
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(2)
            try:
                url = window.get_current_url() or ""
            except Exception:
                break
            # Still on the identity provider — user has not finished signing in.
            if "extlogon.aveva.com" in url:
                continue
            try:
                window.evaluate_js(_CAPTURE_JS)
                window.evaluate_js(_NUDGE_JS)
                time.sleep(2)
                found = json.loads(window.evaluate_js(_READ_JS) or "[]")
            except Exception:
                continue
            if found:
                captured["token"] = _extract(found[0])
                if push_url:
                    captured["cookies"] = _cookies_from_window(window)
                break
        try:
            window.destroy()
        except Exception:
            pass

    window = webview.create_window("Sign in to AVEVA Support", HOME, width=1100, height=850)
    webview.start(watch, window)

    token = captured.get("token")
    if not token:
        raise SystemExit(
            "No token captured. Make sure you completed sign-in before the window "
            "closed, then run `pi-kb-mcp login` again."
        )

    if push_url:
        import os
        secret = os.environ.get("PI_KB_MCP_SECRET", "")
        if not secret:
            raise SystemExit(
                "Set PI_KB_MCP_SECRET to the same secret your server uses before "
                "pushing a session."
            )
        push_session(push_url, secret, captured.get("cookies") or [])

    expires_at = token_expiry(token)
    path = save_token(token, expires_at)
    when = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(expires_at))
        if expires_at else "unknown"
    )
    print(f"Session saved to {path} (mode 0600). Valid until {when}.")
    return token
