"""Unit tests. No network and no credentials — see NOTES.md for live shapes."""

import ast
import json
import os
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
        src = (Path(__file__).parent / "src" / "pi_kb_mcp").glob("*.py")
        for path in src:
            if path.name == "login.py":
                continue  # the login command is allowed a web view
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
