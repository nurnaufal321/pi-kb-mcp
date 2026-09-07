"""Sign in to the AVEVA portal headlessly from a username and password.

Mode B only, and reached only from the phone login endpoint. This is the one
module in the project that ever sees a password, and it is written so that the
password lives no longer than the request that carried it:

  - never written to disk, never logged, never placed in a URL or a query string;
  - never interpolated into an exception message or a response body;
  - typed into an ephemeral browser context that is destroyed with the request,
    with tracing, video and HAR capture all left off;
  - only the resulting cookies outlive the call.

One honest limit: Python cannot guarantee a str is wiped from memory, so the
value lingers in the process until the allocator reuses that space. That is a
property of the runtime, not a choice made here.

Why a browser at all, when there is no MFA to clear: the session cookie is
HttpOnly and is only minted by completing the identity provider's own redirect
chain. Driving the real form is the reliable way to get one. Chromium is already
in the serve image for refresh.py, so this costs no new dependency.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from .session_store import SESSION_COOKIE, relevant

log = logging.getLogger(__name__)

HOME = "https://softwaresupportsp.aveva.com/en-US/home"

# The identity provider may present username and password on one page (classic
# ADFS) or across two (username, then password). Both shapes are handled, since
# this code cannot see the live page to bet on one.
# Tried in order. A comma-separated selector would resolve by DOM position
# instead, which picks wrong on any page that also carries a search box.
USER_FIELDS = (
    "input[name='UserName']",
    "#userNameInput",
    "input[type=email]",
    "input[name*='user' i]",
    "input[type=text]",
)
# Used only to wait for the form to finish rendering, before choosing among them.
ANY_USER_FIELD = ", ".join(USER_FIELDS)
PASSWORD_FIELD = "input[type=password]"

# Enter alone is not always enough: a browser refuses implicit submission when a
# field fails its own HTML validation (a type=email input holding a username
# that is not an email address, say), and the form then sits there silently.
SUBMIT_CONTROLS = (
    "button[type=submit], input[type=submit], #submitButton, "
    "button:not([type=button]):not([type=reset])"
)

# 'Keep me signed in' is what makes the session outlive the browser, and it is
# the difference between re-authenticating in days rather than hours. Matched by
# name rather than by position, so an unrelated consent checkbox is never ticked.
KMSI_FIELDS = (
    "input[name='Kmsi']", "#kmsiInput", "input[name='KMSI']",
    "input[type=checkbox][name*='keep' i]",
)


class LoginError(RuntimeError):
    """Sign-in did not produce a session.

    The message is shown to the user and never contains their credentials.
    """


async def _visible(page, selector: str, timeout_ms: int):
    """First visible match, or None once the timeout is spent."""
    try:
        locator = page.locator(selector).filter(visible=True).first
        await locator.wait_for(state="visible", timeout=timeout_ms)
        return locator
    except Exception:
        return None


async def _first_of(page, selectors, timeout_ms: int = 500):
    """First selector that matches something visible, in the order given."""
    for selector in selectors:
        found = await _visible(page, selector, timeout_ms)
        if found is not None:
            return found
    return None


async def _submit(page, field, timeout_ms: int = 3_000) -> None:
    """Advance past a form step, tolerating a form that ignores Enter.

    Tries Enter first because it works regardless of how the submit control is
    labelled, then falls back to clicking one if the page did not move.
    """
    before = page.url
    try:
        await field.press("Enter")
    except Exception:
        pass

    try:
        await page.wait_for_url(lambda url: url != before, timeout=timeout_ms)
        return
    except Exception:
        pass

    # The form may have submitted without changing the URL. Clicking again
    # would submit twice, so only fall back while the control we pressed Enter
    # in is still sitting there untouched.
    try:
        if not await field.is_visible():
            return
    except Exception:
        return

    button = await _visible(page, SUBMIT_CONTROLS, timeout_ms)
    if button is not None:
        try:
            await button.click()
        except Exception:
            pass


async def _validation_hint(field) -> str:
    """Surface a browser validation message, if one is blocking submission.

    A field that fails its own HTML validation stops the form submitting by any
    means — Enter and a button click alike — and the page simply sits there.
    Without this the symptom looks identical to AVEVA changing their markup.
    """
    try:
        message = (await field.evaluate("el => el.validationMessage || ''") or "").strip()
    except Exception:
        return ""
    return f' The username field reports: "{message[:120]}".' if message else ""


async def _describe(page) -> str:
    """Say where sign-in actually ended up, without echoing any input.

    This is the difference between a five-minute fix and an evening of guessing
    when AVEVA changes something: report the page that was reached, not just
    that the attempt failed.
    """
    bits: list[str] = []

    # Path only. A query string can carry tokens, so it is deliberately dropped.
    try:
        parts = urlsplit(page.url)
        bits.append(f"at {parts.netloc}{parts.path}")
    except Exception:
        pass

    for label, selector in (
        ("error", "#errorText, [role=alert], .error, .alert"),
        ("heading", "h1, h2"),
    ):
        try:
            found = page.locator(selector).filter(visible=True).first
            if await found.count():
                text = (await found.inner_text()).strip().replace("\n", " ")
                if text:
                    bits.append(f'{label}: "{text[:200]}"')
        except Exception:
            continue

    try:
        title = (await page.title() or "").strip()
        if title:
            bits.append(f'title: "{title[:120]}"')
    except Exception:
        pass

    return "; ".join(bits) or "no identifiable page"


async def sign_in(username: str, password: str, timeout_ms: int = 60_000) -> list[dict]:
    """Drive the portal's sign-in form and return the resulting session cookies.

    Raises LoginError, whose message describes the page reached rather than
    anything that was typed into it.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:  # pragma: no cover - depends on optional extra
        raise LoginError(
            "Playwright is not installed. This server was built without the "
            "'serve' extra, so it cannot sign in on your behalf."
        ) from None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        try:
            # Ephemeral context: no persistent profile, no tracing, no video,
            # no HAR. Nothing that was typed here survives browser.close().
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(HOME, wait_until="domcontentloaded", timeout=timeout_ms)

            # Wait for the form to render, then choose the username field by
            # priority rather than by where it happens to sit in the document.
            if await _visible(page, ANY_USER_FIELD, timeout_ms) is None:
                raise LoginError(
                    "Could not find a username field on AVEVA's sign-in page "
                    f"({await _describe(page)}). The page may have changed shape."
                )
            user_field = await _first_of(page, USER_FIELDS)
            if user_field is None:  # pragma: no cover - lost between the two waits
                raise LoginError(
                    f"AVEVA's sign-in page changed while loading ({await _describe(page)})."
                )
            await user_field.fill(username)

            password_field = await _visible(page, PASSWORD_FIELD, 5_000)
            if password_field is None:
                # Two-step flow: the password page follows the username.
                await _submit(page, user_field)
                password_field = await _visible(page, PASSWORD_FIELD, timeout_ms)
            if password_field is None:
                hint = await _validation_hint(user_field)
                raise LoginError(
                    "AVEVA never presented a password field "
                    f"({await _describe(page)}).{hint} Check the username is correct."
                )
            await password_field.fill(password)

            for selector in KMSI_FIELDS:
                box = await _visible(page, selector, 500)
                if box is not None:
                    try:
                        await box.check()
                    except Exception:
                        pass
                    break

            await _submit(page, password_field)

            # Success is the session cookie appearing, not a particular
            # navigation: the chain ends in redirects this code does not model.
            cookies: list[dict] = []
            for _ in range(int(timeout_ms / 1000)):
                cookies = await context.cookies()
                if any(c.get("name") == SESSION_COOKIE for c in cookies):
                    break
                await page.wait_for_timeout(1000)
            else:
                raise LoginError(f"Sign-in did not complete — {await _describe(page)}.")
        finally:
            await browser.close()

    kept = relevant(cookies)
    # Count only. These are credentials; the values are never logged.
    log.info("signed in to the portal and captured %d session cookies", len(kept))
    return kept
