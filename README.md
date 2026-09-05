# PI KB MCP

**Ground your AI answers in AVEVA's Customer Support knowledge base.**

An [MCP](https://modelcontextprotocol.io) server that searches and reads knowledge
base articles from the AVEVA Customer Support Portal — tech notes, known issues,
error-message diagnostics and workarounds that do **not** appear in the public
product documentation.

Companion to [pi-doc-mcp](https://github.com/nurnaufal321/pi-doc-mcp), which covers
the public product manuals. Use both: manuals for how things work, KB for when
they don't.

## Requires an AVEVA support entitlement

KB articles are licensed to your support contract. You sign in with your own AVEVA
account and see exactly what that account is entitled to.

## Install

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
git clone https://github.com/nurnaufal321/pi-kb-mcp.git
cd pi-kb-mcp
uv sync --extra login
```

Register it, replacing the path with wherever you cloned it:

```bash
claude mcp add pi-kb --scope user -- uv run --directory /path/to/pi-kb-mcp pi-kb-mcp
```

Then sign in:

```bash
uv run pi-kb-mcp login
```

`pi-kb-mcp login` opens a window on **AVEVA's own sign-in page**. Your password is
never seen, handled or stored by this tool — it is typed into AVEVA's page. Only the
resulting session token is cached, at `~/.config/pi-kb-mcp/session.json` (mode 0600).

Sessions last about 8 hours; re-run `pi-kb-mcp login` when a tool tells you to.

## Tools

| Tool | What it does |
|---|---|
| `search_pi_kb` | Search KB articles by error message or symptom |
| `get_kb_article` | Read one article in full by number or URL |
| `list_kb_products` | List product names for narrowing a search |

## Configuration

| Variable | Purpose |
|---|---|
| `AVEVA_KB_TOKEN` | Use this bearer token instead of the cached session. Fallback for headless machines and Linux without GTK/WebKit. |
| `PI_KB_MCP_SESSION` | Override the session file location. |

## Privacy

The server process reads a token file and talks to AVEVA. That's all. It never
touches your browser's cookie store or keychain, has no telemetry, and sends
nothing anywhere except `softwaresupportsp.aveva.com` and
`services.softwaresupport.aveva.com`.

Don't share your session token, and don't expose this server on a network — anyone
who reaches it would be spending your support entitlement under your identity.

## Development

```bash
uv sync --extra login
uv run pytest
```

API shapes are documented in [NOTES.md](NOTES.md).

## License

MIT
