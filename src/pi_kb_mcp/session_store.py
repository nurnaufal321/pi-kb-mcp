"""Where Mode B keeps the portal session cookies.

Stored as JSON in a directory that should be a Docker volume, never the image.
Written 0600, and never logged — callers must not echo these values.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

STORE_PATH = Path(
    os.environ.get("PI_KB_MCP_COOKIES")
    or Path.home() / ".config" / "pi-kb-mcp" / "cookies.json"
)

# Cookies the portal login actually needs; anything else (analytics, consent)
# is dropped so we persist as little as possible.
KEEP_PREFIXES = ("FedAuth", "rtFa", "EdgeAccessCookie", "SPOIDCRL")


def relevant(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only session cookies, discarding analytics and consent cookies."""
    return [
        c for c in cookies
        if any(str(c.get("name", "")).startswith(p) for p in KEEP_PREFIXES)
    ]


def save_cookies(cookies: list[dict[str, Any]]) -> Path:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(STORE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(cookies, fh)
    return STORE_PATH


def load_cookies() -> list[dict[str, Any]]:
    try:
        data = json.loads(STORE_PATH.read_text())
    except (FileNotFoundError, OSError, ValueError):
        return []
    return data if isinstance(data, list) else []
