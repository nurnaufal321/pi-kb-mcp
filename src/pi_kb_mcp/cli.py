"""Command line entry point: `pi-kb-mcp` serves, `pi-kb-mcp login` authenticates."""

import sys


def main() -> None:
    args = sys.argv[1:]

    if args and args[0] == "login":
        from .login import run_login
        run_login()
        return

    if args and args[0] in {"-h", "--help", "help"}:
        print(__doc__.strip())
        print("\n  pi-kb-mcp          Run the MCP server over stdio")
        print("  pi-kb-mcp login    Sign in and cache a session token")
        return

    from .server import run
    run()
