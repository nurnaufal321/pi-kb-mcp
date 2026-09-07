"""Unit tests. No network and no credentials — see NOTES.md for live shapes."""

import ast
import asyncio
import json
import os
import sys
import stat
import tempfile
import time
from pathlib import Path

import pytest

from pi_kb_mcp import auth, server


class TestArticleNumber:
    @pytest.mark.parametrize("value,expected", [
        ("000100024", "000100024"),
        ("  000092765  ", "000092765"),
        ("https://softwaresupportsp.aveva.com/en-US/knowledge/details/000100024", "000100024"),
        ("https://softwaresupportsp.aveva.com/en-US/knowledge/details/000092765?lang=en_US", "000092765"),
    ])
    def test_accepts(self, value, expected):
        assert server.article_number(value) == expected

    @pytest.mark.parametrize("value", ["", "not-an-article", "12345", "https://example.com/x"])
    def test_rejects(self, value):
        assert server.article_number(value) is None


class TestFilters:
    def test_always_scopes_to_english_kb(self):
        groups = {f["groupName"] for f in server._filters(None)}
        assert groups == {"Languages", "Content Source"}
        source = next(f for f in server._filters(None) if f["groupName"] == "Content Source")
        assert source["values"][0]["name"] == server.KB_SOURCE

    def test_products_use_avevas_misspelled_field(self):
        """AVEVA's API spells the Products facet 'cs_synonymns'; verified live."""
        products = next(
            f for f in server._filters(["PI Vision"]) if f["groupName"] == "Products"
        )
        assert products["fieldName"] == "cs_synonymns"
        assert products["values"] == [{"name": "PI Vision", "isSelected": True}]

    def test_no_products_group_when_unscoped(self):
        assert all(f["groupName"] != "Products" for f in server._filters(None))


class TestHtmlToText:
    def test_strips_markup_and_scripts(self):
        html = "<h3>Issue</h3><p>Broken</p><script>evil()</script>"
        text = server.html_to_text(html)
        assert "Issue" in text and "Broken" in text
        assert "evil" not in text and "<" not in text


class TestSession:
    def test_saved_token_is_owner_only(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            monkeypatch.setattr(auth, "SESSION_PATH", path)
            auth.save_token("abc", 999)
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
            assert json.loads(path.read_text())["token"] == "abc"

    def test_env_token_wins(self, monkeypatch):
        monkeypatch.setenv("AVEVA_KB_TOKEN", "from-env")
        assert auth.get_token() == "from-env"

    def test_missing_session_explains_how_to_fix(self, monkeypatch):
        monkeypatch.delenv("AVEVA_KB_TOKEN", raising=False)
        monkeypatch.setattr(auth, "SESSION_PATH", Path("/nonexistent/session.json"))
        with pytest.raises(auth.AuthError, match="pi-kb-mcp login"):
            auth.get_token()

    def test_expired_session_explains_how_to_fix(self, monkeypatch):
        monkeypatch.delenv("AVEVA_KB_TOKEN", raising=False)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            monkeypatch.setattr(auth, "SESSION_PATH", path)
            auth.save_token("stale", 1)
            with pytest.raises(auth.AuthError, match="expired"):
                auth.get_token()

    def test_expiry_read_from_jwt(self):
        import base64
        payload = base64.urlsafe_b64encode(b'{"exp":1788634403}').decode().rstrip("=")
        assert auth.token_expiry(f"h.{payload}.sig") == 1788634403
        assert auth.token_expiry("not-a-jwt") is None


class TestServerSurface:
    """Guards the properties the Glama score and the security scan depend on."""

    def test_server_process_never_reads_credential_stores(self):
        # login.py (desktop web view), refresh.py (Mode B headless refresh) and
        # portal_login.py (Mode B phone sign-in) are the only modules allowed a
        # browser. None is imported by the stdio server: refresh.py is imported
        # lazily and only when Mode B sets PI_KB_MCP_SELF_REFRESH, and
        # portal_login.py only from the Mode B /login route.
        allowed = {"login.py", "refresh.py", "portal_login.py"}
        src = (Path(__file__).parent / "src" / "pi_kb_mcp").glob("*.py")
        for path in src:
            if path.name in allowed:
                continue
            tree = ast.parse(path.read_text())
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            banned = {"keyring", "browser_cookie3", "rookiepy", "rookie", "webview",
                      "playwright", "secretstorage"}
            assert not (imported & banned), f"{path.name} imports {imported & banned}"

    def test_stdio_server_import_graph_has_no_browser(self):
        """The default server must not pull in a browser, even transitively."""
        import subprocess
        code = (
            "import sys, pi_kb_mcp.server;"
            "mods = set(sys.modules);"
            "bad = {m for m in mods if m.split('.')[0] in "
            "{'playwright', 'webview', 'keyring', 'browser_cookie3'}};"
            "print(sorted(bad))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        assert out.stdout.strip() == "[]", f"browser modules loaded: {out.stdout}"

    @pytest.mark.anyio
    async def test_three_read_only_tools(self):
        tools = await server.mcp.list_tools()
        assert {t.name for t in tools} == {
            "search_pi_kb", "get_kb_article", "list_kb_products"
        }
        for tool in tools:
            assert tool.annotations and tool.annotations.read_only_hint
            assert tool.description and len(tool.description) > 200

    @pytest.fixture
    def anyio_backend(self):
        return "asyncio"


class TestModeBGate:
    """Mode B is private by construction; these guard that."""

    def test_refuses_to_start_without_a_strong_secret(self, monkeypatch):
        from pi_kb_mcp import http_app

        for value in ("", "short", "x" * 23):
            monkeypatch.setenv(http_app.SECRET_ENV, value)
            with pytest.raises(SystemExit):
                http_app._secret()

        monkeypatch.setenv(http_app.SECRET_ENV, "y" * 24)
        assert http_app._secret() == "y" * 24

    def test_only_session_cookies_are_persisted(self):
        from pi_kb_mcp.session_store import relevant

        kept = relevant([
            {"name": "_ga", "value": "analytics"},
            {"name": "notice_behavior", "value": "consent"},
            {"name": "FedAuth", "value": "session"},
        ])
        assert [c["name"] for c in kept] == ["FedAuth"]

    def test_cookies_written_owner_only(self, monkeypatch):
        from pi_kb_mcp import session_store

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.json"
            monkeypatch.setattr(session_store, "STORE_PATH", path)
            session_store.save_cookies([{"name": "FedAuth", "value": "x"}])
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
            assert session_store.load_cookies()[0]["name"] == "FedAuth"

    def test_missing_cookie_store_reads_empty(self, monkeypatch):
        from pi_kb_mcp import session_store

        monkeypatch.setattr(session_store, "STORE_PATH", Path("/nonexistent/c.json"))
        assert session_store.load_cookies() == []

    def test_self_refresh_is_off_unless_mode_b_sets_it(self, monkeypatch):
        monkeypatch.delenv("PI_KB_MCP_SELF_REFRESH", raising=False)
        assert auth._refresh_enabled() is False
        monkeypatch.setenv("PI_KB_MCP_SELF_REFRESH", "1")
        assert auth._refresh_enabled() is True


class TestSessionRollsForward:
    """Part 1: a successful mint must extend the stored session, never destroy it."""

    def _store(self, monkeypatch, tmp):
        from pi_kb_mcp import session_store
        path = Path(tmp) / "cookies.json"
        monkeypatch.setattr(session_store, "STORE_PATH", path)
        return session_store, path

    def test_successful_boot_persists_the_freshened_cookies(self, monkeypatch):
        from pi_kb_mcp import refresh, session_store

        with tempfile.TemporaryDirectory() as tmp:
            store, path = self._store(monkeypatch, tmp)
            store.save_cookies([{"name": session_store.SESSION_COOKIE, "value": "old"}])

            refresh._roll_session_forward([
                {"name": session_store.SESSION_COOKIE, "value": "new"},
                {"name": "_ga", "value": "analytics"},
            ])

            kept = store.load_cookies()
            assert [c["name"] for c in kept] == [session_store.SESSION_COOKIE]
            assert kept[0]["value"] == "new", "the rolled-forward cookie should win"

    def test_never_clobbers_a_good_jar_with_an_anonymous_one(self, monkeypatch):
        """The failure that would strand the server with no way back."""
        from pi_kb_mcp import refresh, session_store

        with tempfile.TemporaryDirectory() as tmp:
            store, path = self._store(monkeypatch, tmp)
            store.save_cookies([{"name": session_store.SESSION_COOKIE, "value": "good"}])

            # A boot that never authenticated: load-balancer affinity only.
            refresh._roll_session_forward([{"name": "ARRAffinity", "value": "x"}])

            kept = store.load_cookies()
            assert kept == [{"name": session_store.SESSION_COOKIE, "value": "good"}]

    def test_empty_cookie_set_leaves_the_store_untouched(self, monkeypatch):
        from pi_kb_mcp import refresh, session_store

        with tempfile.TemporaryDirectory() as tmp:
            store, path = self._store(monkeypatch, tmp)
            store.save_cookies([{"name": session_store.SESSION_COOKIE, "value": "good"}])
            refresh._roll_session_forward([])
            assert store.load_cookies()[0]["value"] == "good"


SENTINEL_PW = "SENTINEL-PW-9f3a2b"
SENTINEL_USER = "SENTINEL-USER-4c1d"
SECRET = "z" * 32


def _login_app():
    """The Mode B routes under test, wired exactly as build_app wires them."""
    from starlette.applications import Starlette
    from starlette.routing import Route

    from pi_kb_mcp import http_app

    app = Starlette(routes=[
        Route("/health", http_app.health, methods=["GET"]),
        Route("/login", http_app.login_page, methods=["GET"]),
        Route("/login", http_app.sign_in, methods=["POST"]),
    ])
    app.add_middleware(http_app.RequireSecret, secret=SECRET)
    return app


class TestPhoneLoginSurface:
    """The /login routes: what is reachable, and by whom."""

    def client(self):
        from starlette.testclient import TestClient
        return TestClient(_login_app())

    def test_form_is_reachable_without_the_secret(self):
        """A phone browser cannot set a bearer header on a plain navigation."""
        r = self.client().get("/login")
        assert r.status_code == 200
        assert "AVEVA password" in r.text

    def test_form_carries_no_secret(self):
        assert SECRET not in self.client().get("/login").text

    def test_the_secret_field_is_saveable_by_a_password_manager(self):
        """The secret is a standing credential, same as any other saved login --
        the annoyance of retyping it every time was a bug, not a safety feature."""
        html = self.client().get("/login").text
        assert 'id="s"' in html
        assert 'autocomplete="off"' not in html.split('id="s"')[1].split(">")[0]

    def test_aveva_fields_refuse_to_be_remembered(self):
        """The opposite property, for the opposite reason: these must never be
        offered to a password manager, matching the 'used once, dropped'
        promise in the README and the footer text on this very page."""
        html = self.client().get("/login").text
        for field_id in ("u", "p"):
            attrs = html.split(f'id="{field_id}"')[1].split(">")[0]
            assert 'autocomplete="off"' in attrs, f"#{field_id} must not be autofillable"

    def test_a_successful_sign_in_explicitly_asks_the_browser_to_remember_it(self):
        """A fetch()-driven login gives most browsers no heuristic signal to
        offer saving a password -- there is no real <form> submit for them to
        observe. navigator.credentials.store() is the only reliable way to ask
        for that prompt from JS. Regression test for the bug where this was
        simply never called, so autocomplete="current-password" alone did
        nothing: no test caught that the first time, hence this one."""
        html = self.client().get("/login").text
        assert "navigator.credentials.store" in html
        assert "PasswordCredential" in html
        # Must be the secret's own value, not the AVEVA password.
        store_call = html[html.index("navigator.credentials.store"):]
        assert "password: s.value" in store_call.split(";")[0] + store_call.split(";")[1]

    def test_secret_and_aveva_fields_sit_in_separate_forms(self):
        """A password manager that sees one <form> with two password fields
        tends to treat it as a change-password form and mismatches which
        credential goes where. Splitting them avoids that entirely."""
        html = self.client().get("/login").text
        secret_form = html[html.index('id="secretForm"'):html.index('id="avevaForm"')]
        assert 'id="s"' in secret_form
        assert 'id="u"' not in secret_form and 'id="p"' not in secret_form

    def test_posting_credentials_requires_the_secret(self):
        r = self.client().post("/login", json={"username": "u", "password": "p"})
        assert r.status_code == 401

    def test_wrong_secret_is_rejected(self):
        r = self.client().post(
            "/login", json={"username": "u", "password": "p"},
            headers={"Authorization": "Bearer " + "q" * 32},
        )
        assert r.status_code == 401

    def test_both_fields_required(self):
        r = self.client().post(
            "/login", json={"username": "u", "password": ""},
            headers={"Authorization": f"Bearer {SECRET}"},
        )
        assert r.status_code == 400

    def test_build_app_registers_both_login_routes(self, monkeypatch):
        from pi_kb_mcp import http_app

        monkeypatch.setenv(http_app.SECRET_ENV, SECRET)
        methods = set()
        for route in http_app.build_app().routes:
            if getattr(route, "path", None) == "/login":
                methods |= set(route.methods or [])
        assert {"GET", "POST"} <= methods


class TestCredentialsAreNotRetained:
    """Credentials must not survive the request that carried them.

    A sentinel password is pushed through the real handler and then hunted for
    in every place it could plausibly come to rest. This exists to fail loudly
    if someone later adds a well-meaning debug log.
    """

    def _run(self, monkeypatch, caplog, tmp, sign_in_impl):
        import logging

        from starlette.testclient import TestClient

        from pi_kb_mcp import http_app, portal_login, session_store

        store = Path(tmp) / "cookies.json"
        monkeypatch.setattr(session_store, "STORE_PATH", store)
        monkeypatch.setattr(portal_login, "sign_in", sign_in_impl)

        with caplog.at_level(logging.DEBUG):
            response = TestClient(_login_app()).post(
                "/login",
                json={"username": SENTINEL_USER, "password": SENTINEL_PW},
                headers={"Authorization": f"Bearer {SECRET}"},
            )
        return response, store, caplog.text

    def test_nothing_typed_reaches_disk_logs_or_the_response(
        self, monkeypatch, caplog
    ):
        from pi_kb_mcp import session_store

        async def fake_sign_in(username, password, **kw):
            assert password == SENTINEL_PW, "the handler must pass the real password"
            return [{"name": session_store.SESSION_COOKIE, "value": "fresh"}]

        with tempfile.TemporaryDirectory() as tmp:
            response, store, logs = self._run(monkeypatch, caplog, tmp, fake_sign_in)

            assert response.status_code == 200
            assert response.json() == {"status": "stored", "cookies": 1}

            haystacks = {
                "response body": response.text,
                "captured logs": logs,
                "cookie store": store.read_text(),
            }
            for where, text in haystacks.items():
                assert SENTINEL_PW not in text, f"password leaked into {where}"
                assert SENTINEL_USER not in text, f"username leaked into {where}"

    def test_a_failed_sign_in_leaks_nothing_and_keeps_the_old_session(
        self, monkeypatch, caplog
    ):
        from pi_kb_mcp import portal_login, session_store

        async def failing_sign_in(username, password, **kw):
            raise portal_login.LoginError(
                'Sign-in did not complete — at extlogon.aveva.com/adfs/ls; '
                'error: "Incorrect user ID or password".'
            )

        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "cookies.json"
            monkeypatch.setattr(session_store, "STORE_PATH", store)
            session_store.save_cookies(
                [{"name": session_store.SESSION_COOKIE, "value": "still-good"}]
            )

            response, store, logs = self._run(
                monkeypatch, caplog, tmp, failing_sign_in
            )

            assert response.status_code == 502
            # The diagnostic must say where it landed...
            assert "extlogon.aveva.com" in response.json()["error"]
            # ...without echoing anything that was typed.
            for where, text in {"response": response.text, "logs": logs}.items():
                assert SENTINEL_PW not in text, f"password leaked into {where}"
                assert SENTINEL_USER not in text, f"username leaked into {where}"
            # And a failure must never cost the working session.
            assert session_store.load_cookies()[0]["value"] == "still-good"

    def test_a_jar_without_a_session_cookie_is_refused(self, monkeypatch, caplog):
        """Guards the abuse case: a bad login must not evict a good session."""
        from pi_kb_mcp import session_store

        async def anonymous_sign_in(username, password, **kw):
            return [{"name": "ARRAffinity", "value": "x"}]

        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "cookies.json"
            monkeypatch.setattr(session_store, "STORE_PATH", store)
            session_store.save_cookies(
                [{"name": session_store.SESSION_COOKIE, "value": "still-good"}]
            )

            response, _, _ = self._run(monkeypatch, caplog, tmp, anonymous_sign_in)

            assert response.status_code == 502
            assert session_store.load_cookies()[0]["value"] == "still-good"


_SHELL = "<html><body>%s</body></html>"

# The two shapes AVEVA's identity provider might present, plus a rejection.
_ONE_PAGE = _SHELL % (
    "<h2>Sign in</h2><form method=post action=/submit>"
    "<input id=userNameInput name=UserName type=text>"
    "<input id=passwordInput name=Password type=password>"
    "<label><input type=checkbox name=Kmsi> Keep me signed in</label>"
    "<button type=submit>Sign in</button></form>"
)
_TWO_PAGE = _SHELL % (
    "<h2>Sign in</h2><form method=post action=/password>"
    "<input id=userNameInput name=UserName type=email>"
    "<button type=submit>Next</button></form>"
)
_PASSWORD_PAGE = _SHELL % (
    "<h2>Enter password</h2><form method=post action=/submit>"
    "<input id=passwordInput name=Password type=password>"
    "<button type=submit>Sign in</button></form>"
)
# A search box sitting before the login form: picking the username field by
# document order instead of by priority types the username into the wrong input.
_SEARCH_FIRST = _SHELL % (
    "<input type=text id=basic-url placeholder=Search>"
    "<h2>Sign in</h2><form method=post action=/submit-checked>"
    "<input id=userNameInput name=UserName type=text>"
    "<input id=passwordInput name=Password type=password>"
    "<button type=submit>Sign in</button></form>"
)
_REJECTED = _SHELL % (
    '<h2>Sign in</h2><div id=errorText>Incorrect user ID or password.</div>'
    "<form method=post action=/rejected>"
    "<input id=userNameInput name=UserName type=text>"
    "<input id=passwordInput name=Password type=password>"
    "<button type=submit>Sign in</button></form>"
)


@pytest.fixture(scope="module")
def portal():
    """A mock identity provider on a real port, for a real browser."""
    pytest.importorskip("playwright")
    import socket
    import threading

    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, RedirectResponse
    from starlette.routing import Route

    from pi_kb_mcp.session_store import SESSION_COOKIE

    async def one_page(request):
        return HTMLResponse(_ONE_PAGE)

    async def two_page(request):
        return HTMLResponse(_TWO_PAGE)

    async def password_page(request):
        await request.form()
        return HTMLResponse(_PASSWORD_PAGE)

    async def search_first(request):
        return HTMLResponse(_SEARCH_FIRST)

    async def submit_checked(request):
        form = await request.form()
        if not str(form.get("UserName") or "").strip():
            return HTMLResponse(_SHELL % "<h2>Username went to the wrong field</h2>")
        return await submit(request)

    async def rejected_page(request):
        return HTMLResponse(_REJECTED)

    async def rejected_submit(request):
        await request.form()
        return HTMLResponse(_REJECTED)

    async def submit(request):
        form = await request.form()
        response = RedirectResponse("/home", status_code=302)
        response.set_cookie(SESSION_COOKIE, "session-xyz", httponly=True, path="/")
        response.set_cookie("_ga", "analytics", path="/")
        response.set_cookie("kmsi", "1" if "Kmsi" in form else "0", path="/")
        return response

    async def home(request):
        return HTMLResponse(_SHELL % "<h1>Portal home</h1>")

    app = Starlette(routes=[
        Route("/one-page", one_page),
        Route("/two-page", two_page),
        Route("/password", password_page, methods=["POST"]),
        Route("/search-first", search_first),
        Route("/rejected-page", rejected_page),
        Route("/rejected", rejected_submit, methods=["POST"]),
        Route("/submit", submit, methods=["POST"]),
        Route("/submit-checked", submit_checked, methods=["POST"]),
        Route("/home", home),
    ])

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


class TestPortalLoginDrivesTheForm:
    """Exercises the real browser code against a mock identity provider.

    Skipped unless the 'serve' extra is installed. Form-driving is the most
    fragile part of Mode B \u2014 it depends on markup this project does not own \u2014
    so it is worth testing both shapes AVEVA might present rather than betting
    on one.
    """

    def _sign_in(self, portal, path, user="probe@example.com", timeout_ms=20_000):
        from pi_kb_mcp import portal_login

        original = portal_login.HOME
        portal_login.HOME = portal + path
        try:
            return asyncio.run(
                portal_login.sign_in(user, SENTINEL_PW, timeout_ms=timeout_ms)
            )
        finally:
            portal_login.HOME = original

    def test_one_page_form(self, portal):
        """Classic ADFS: username, password and 'keep me signed in' together."""
        from pi_kb_mcp.session_store import SESSION_COOKIE

        cookies = {c["name"]: c["value"] for c in self._sign_in(portal, "/one-page")}
        assert SESSION_COOKIE in cookies
        assert "_ga" not in cookies, "analytics cookies must not be persisted"
        assert cookies.get("kmsi") == "1", "'keep me signed in' lengthens the session"

    def test_two_page_form(self, portal):
        """The other shape: username first, password on the page that follows."""
        from pi_kb_mcp.session_store import SESSION_COOKIE

        cookies = {c["name"]: c["value"] for c in self._sign_in(portal, "/two-page")}
        assert SESSION_COOKIE in cookies

    def test_a_search_box_before_the_form_is_not_mistaken_for_the_username(
        self, portal
    ):
        """Fields are chosen by priority, not by position in the document."""
        from pi_kb_mcp.session_store import SESSION_COOKIE

        cookies = {c["name"]: c["value"] for c in self._sign_in(portal, "/search-first")}
        assert SESSION_COOKIE in cookies

    def test_rejected_credentials_report_avevas_own_error(self, portal):
        from pi_kb_mcp import portal_login

        with pytest.raises(portal_login.LoginError) as caught:
            self._sign_in(portal, "/rejected-page", timeout_ms=6_000)
        message = str(caught.value)
        assert "Incorrect user ID or password" in message
        assert SENTINEL_PW not in message

    def test_a_field_that_blocks_submission_says_so(self, portal):
        """Otherwise this is indistinguishable from AVEVA changing their markup."""
        from pi_kb_mcp import portal_login

        with pytest.raises(portal_login.LoginError) as caught:
            self._sign_in(portal, "/two-page", user="not-an-email", timeout_ms=6_000)
        assert "username field reports" in str(caught.value)
