"""Mint a fresh bearer token from stored portal cookies, headlessly.

Mode B only. The portal's token-issuer endpoint cannot be called directly (see
NOTES.md), and the SPA keeps its JWT in memory, so the only way to obtain a token
without a human is to let the real application boot and observe the request it
makes. This loads the SPA in headless Chromium with the stored session cookies
injected, then reads the Authorization header off the app's own API call.

Requires the `serve` extra (Playwright). Not imported by the stdio server.
"""

from __future__ import annotations

import logging

from .session_store import load_cookies

log = logging.getLogger(__name__)

HOME = "https://softwaresupportsp.aveva.com/en-US/home"
API_HOST = "services.softwaresupport.aveva.com"


class RefreshError(RuntimeError):
    """Raised when no token could be minted. Message is user-facing."""


async def mint_token(timeout_ms: int = 45_000) -> str:
    """Boot the portal SPA headlessly and capture the bearer it sends."""
    cookies = load_cookies()
    if not cookies:
        raise RefreshError(
            "No portal session stored. Run `pi-kb-mcp login --push <url>` from a "
            "machine with a browser to seed this server."
        )

    try:
        from playwright.async_api import async_playwright
    except ImportError:  # pragma: no cover - depends on optional extra
        raise RefreshError(
            "Playwright is not installed. This server was built without the "
            "'serve' extra, so it cannot refresh its own token."
        ) from None

    captured: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        try:
            context = await browser.new_context()
            await context.add_cookies(cookies)

            def on_request(request) -> None:
                if API_HOST not in request.url:
                    return
                value = request.headers.get("authorization")
                if value and value.lower().startswith("bearer "):
                    captured.append(value[7:].strip())

            context.on("request", on_request)
            page = await context.new_page()
            await page.goto(HOME, wait_until="domcontentloaded", timeout=timeout_ms)

            # The SPA fires several authenticated calls during boot; wait for one.
            await page.wait_for_timeout(1500)
            for _ in range(int(timeout_ms / 1000)):
                if captured:
                    break
                await page.wait_for_timeout(1000)
        finally:
            await browser.close()

    if not captured:
        raise RefreshError(
            "Portal session has expired — the stored cookies no longer sign in. "
            "Run `pi-kb-mcp login --push <url>` again from a machine with a browser."
        )

    log.info("minted a fresh portal token")
    return captured[0]
