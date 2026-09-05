"""Command line entry point.

  pi-kb-mcp                     Run the MCP server over stdio (Mode A, default)
  pi-kb-mcp login               Sign in and cache a session token
  pi-kb-mcp login --push URL    Also push the session to a private Mode B server
  pi-kb-mcp serve               Run the private HTTP server (Mode B)
"""

import os
import sys

USAGE = __doc__.strip()


def main() -> None:
    args = sys.argv[1:]

    if args and args[0] == "login":
        push_url = None
        if "--push" in args:
            i = args.index("--push")
            if i + 1 >= len(args):
                sys.exit("--push needs a URL, e.g. --push https://kb.example.com")
            push_url = args[i + 1]
        from .login import run_login
        run_login(push_url=push_url)
        return

    if args and args[0] == "serve":
        # Mode B holds portal cookies and may mint its own tokens.
        os.environ["PI_KB_MCP_SELF_REFRESH"] = "1"
        from .http_app import run
        run()
        return

    if args and args[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return

    from .server import run
    run()
