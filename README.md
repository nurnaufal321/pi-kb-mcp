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
never seen, handled or stored by this command — it is typed into AVEVA's page. Only
the resulting session token is cached, at `~/.config/pi-kb-mcp/session.json` (mode 0600).

(The optional Mode B phone login below is the one exception in this project, and it
says so plainly.)

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
| `PI_KB_MCP_SECRET` | Mode B only. Shared secret gating the HTTP server; 24 characters minimum. |
| `PI_KB_MCP_COOKIES` | Mode B only. Where pushed portal cookies are stored. |
| `PI_KB_MCP_ALLOWED_HOSTS` | Mode B only. Comma-separated hostnames to accept, e.g. `kb.example.com`. Unset disables the SDK's DNS-rebinding check, which is redundant behind the shared secret. |
| `PI_KB_BIND` | Mode B only. Host bind, if you uncomment the `ports` section. Unused by default. |

## Phone access (optional, private)

The steps above are all you need on a laptop. If you also want to reach the KB
from your phone, `pi-kb-mcp serve` runs a **private, single-user** HTTP server
you host yourself.

It holds exactly one AVEVA session — yours. Signing in is gated behind the shared
secret, so it never asks an unauthenticated caller for anything.

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'   # your secret
PI_KB_MCP_SECRET=<secret> docker compose up -d                 # see docker-compose.yml
```

Then seed it from your laptop, which is the only machine that can sign in:

```bash
PI_KB_MCP_SECRET=<secret> uv run pi-kb-mcp login --push https://kb.example.com
```

Add it in your client as a remote MCP server at `https://kb.example.com/mcp`
with header `Authorization: Bearer <secret>`.

**How long it lasts.** The bearer token AVEVA issues lives 8 hours, so the server
re-mints one itself: it stores your portal cookies and boots the portal in headless
Chromium to obtain a fresh token — the token issuer cannot be called directly (see
[NOTES.md](NOTES.md)). Booting the real app also rolls the session cookie forward,
which the server keeps, so the session extends itself for as long as the server
keeps refreshing. AVEVA decides whether that expiry slides or is absolute, so this
is not a guarantee of forever.

**When it does lapse**, you no longer need a laptop. Open `https://kb.example.com/login`
on your phone, enter the shared secret and your AVEVA username and password, and the
server signs itself back in and stores the new cookies.

### What the login page does with your credentials

They are used once, to drive AVEVA's own sign-in form in a headless browser, and
then dropped. They are never written to disk, never logged, never put in a URL and
never echoed back — a test pushes a sentinel password through the handler and fails
if it turns up in the cookie store, the logs or the response.

Two things it would be dishonest not to say:

- **Your password passes through this server**, which is not true of `pi-kb-mcp login`
  on a laptop. It is not stored, but Python cannot guarantee a string is scrubbed
  from memory, so it lingers in the process until that memory is reused. If you would
  rather that never happen, don't use this page — `login --push` from a laptop still works.
- **If you front this with a tunnel that terminates TLS** (Cloudflare does), your
  password is decrypted at that edge. You already trust it with your bearer secret
  and every KB query; an account password is a bigger thing to hand over. Decide that
  deliberately.

A failed sign-in leaves your existing session untouched, and reports which page AVEVA
actually returned — so a changed form or a wrong password says so, instead of failing
silently.

### Read this before hosting it

- **Never share the URL or the secret, and never advertise it publicly.** Anyone who
  has both queries the KB as *you* — your entitlement, your identity, in AVEVA's logs.
  Sharing it looks exactly like you scraping the KB, which is how support accounts get
  suspended. Point other people at this repo instead; they run their own.
- **Always put TLS in front of it.** `docker-compose.yml` binds to loopback for that reason.
- **This box now holds a long-lived credential of yours.** If it is compromised, your
  AVEVA session goes with it. That is the cost of phone access, and it is the reason
  Mode B is opt-in rather than the default.
- The server refuses to start without a 24+ character secret, rejects every
  unauthenticated request, and never logs cookie values.

## Privacy

The server process reads a token file and talks to AVEVA. That's all. It never
touches your browser's cookie store or keychain, has no telemetry, and sends
nothing anywhere except `softwaresupportsp.aveva.com` and
`services.softwaresupport.aveva.com`. If you use the Mode B login page, the
credentials you type there go to AVEVA's sign-in form and nowhere else, and are
not retained afterwards.

Don't share your session token. The default stdio server is not networked at all;
if you enable the optional phone access above, keep its URL and secret private —
anyone who reaches it spends your support entitlement under your identity.

## Development

```bash
uv sync --extra login
uv run pytest
```

API shapes are documented in [NOTES.md](NOTES.md).

## License

MIT
