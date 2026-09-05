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

# Analytics and consent cookies, dropped so we persist as little as possible.
# This is a denylist rather than an allowlist on purpose: an allowlist of
# guessed session-cookie names silently discarded everything and left the
# server with no session at all.
DROP_PREFIXES = (
    "_ga", "_gid", "_gat", "_fbp", "_uet",
    "notice_", "cmapi_", "TAsessionID", "OptanonConsent", "OptanonAlertBox",
    "AMCV_", "AMCVS_", "s_cc", "s_sq", "mbox",
)


def relevant(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop analytics and consent cookies, keep everything else."""
    return [
        c for c in cookies
        if c.get("name")
        and not any(str(c["name"]).startswith(p) for p in DROP_PREFIXES)
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
