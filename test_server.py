"""Unit tests. No network and no credentials — see NOTES.md for live shapes."""

import ast
import json
import os
import sys
import stat
import tempfile
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
        # login.py (desktop web view) and refresh.py (Mode B headless browser)
        # are the only modules allowed a browser. Neither is imported by the
        # stdio server: refresh.py is imported lazily, and only when Mode B
        # sets PI_KB_MCP_SELF_REFRESH.
        allowed = {"login.py", "refresh.py"}
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
