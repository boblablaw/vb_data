"""Shared real-Chrome fetch for stats.ncaa.org.

stats.ncaa.org (Akamai) blocks Playwright's *bundled* Chromium outright ("Access
Denied"), but serves normally to a real Chrome install (``channel="chrome"``) with the
``navigator.webdriver`` flag masked. This module centralizes that configuration so the
scraper and the discovery/game-stats scripts all present identical, unblocked traffic and
pace themselves conservatively.

A single Chrome page is started lazily and reused across calls; it is torn down at exit.

NOTE: carried over verbatim from the original vb_scraper repo — this is the load-bearing
Akamai bypass. Do not "clean it up" without re-verifying against the live site.
"""
from __future__ import annotations

import atexit
import logging
import random
import time
from collections.abc import Iterable

from ..config import settings

log = logging.getLogger(__name__)

# Current desktop Chrome UA. Keep in step with the installed Chrome major version.
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)

# Init script injected into every context to hide the automation flag Akamai checks.
WEBDRIVER_MASK_JS = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
)

# --------- tunable config (from settings/env; overridable by callers before first fetch) ---------
HEADLESS = settings.vb_headless
CHANNEL = settings.vb_chrome_channel or None   # real Chrome by default; "" -> bundled/executable
EXECUTABLE_PATH = settings.vb_chrome_executable  # e.g. /usr/bin/chromium-browser on ARM hosts
USER_AGENT = DEFAULT_UA
HUMAN_DELAY_RANGE = (settings.vb_min_delay, settings.vb_max_delay)
TIMEOUT = 45                       # seconds per navigation
NETWORKIDLE_MS = 6000              # best-effort settle budget (analytics beacons never idle)
FETCH_RETRIES = settings.vb_fetch_retries          # attempts per page before giving up
RETRY_BACKOFF = settings.vb_fetch_retry_backoff    # base seconds between attempts (grows per attempt)

# Extra flags that quiet the most common automation signals. --no-sandbox is required when
# running as root in a container/VM; the AutomationControlled flag is what tools like Akamai
# BotManager look for.
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]

try:
    from playwright.sync_api import (
        TimeoutError as PlaywrightTimeoutError,
    )
    from playwright.sync_api import (
        sync_playwright,
    )
except ImportError:  # pragma: no cover - optional dependency
    sync_playwright = None  # type: ignore
    PlaywrightTimeoutError = Exception  # type: ignore

_PLAYWRIGHT = None
_BROWSER = None
_PAGE = None


def human_pause() -> None:
    """Sleep a random amount to mimic human navigation cadence."""
    low, high = HUMAN_DELAY_RANGE
    time.sleep(random.uniform(low, high))


def get_page():
    """Lazily start and return a single reusable real-Chrome page."""
    global _PLAYWRIGHT, _BROWSER, _PAGE
    if _PAGE is not None:
        return _PAGE
    if not sync_playwright:
        raise RuntimeError("playwright is not installed; cannot fetch from stats.ncaa.org")
    _PLAYWRIGHT = sync_playwright().start()
    launch_kwargs: dict = {"headless": HEADLESS}
    if EXECUTABLE_PATH:
        # Non-default browser (e.g. system Chromium on a server): it needs the sandbox/stealth
        # flags. The proven real-Chrome path below is left exactly as it was.
        launch_kwargs["executable_path"] = EXECUTABLE_PATH
        launch_kwargs["args"] = LAUNCH_ARGS
    elif CHANNEL:
        launch_kwargs["channel"] = CHANNEL
    else:
        launch_kwargs["args"] = LAUNCH_ARGS
    try:
        _BROWSER = _PLAYWRIGHT.chromium.launch(**launch_kwargs)
    except Exception as e:
        _BROWSER = None
        # The usual culprit on a headless server is a headed launch (VB_HEADLESS=false) with no
        # X server — Playwright refuses to start. Retry headless with the SAME real Chrome binary:
        # that keeps the Akamai bypass intact while dropping the display requirement, so `vb`
        # commands work without xvfb. Only if that also fails do we drop to bundled Chromium.
        if not launch_kwargs.get("headless"):
            log.warning("headed browser launch failed (%s); retrying headless with %s",
                        e, EXECUTABLE_PATH or CHANNEL or "bundled Chromium")
            launch_kwargs["headless"] = True
            try:
                _BROWSER = _PLAYWRIGHT.chromium.launch(**launch_kwargs)
            except Exception as e2:
                e = e2
        if _BROWSER is None:
            log.warning("browser launch failed (%s); falling back to bundled Chromium (headless) — "
                        "stats.ncaa.org may return 'Access Denied'", e)
            _BROWSER = _PLAYWRIGHT.chromium.launch(headless=True, args=LAUNCH_ARGS)
    context = _BROWSER.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
        extra_http_headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    context.add_init_script(WEBDRIVER_MASK_JS)
    _PAGE = context.new_page()
    return _PAGE


def shutdown() -> None:
    global _PLAYWRIGHT, _BROWSER, _PAGE
    try:
        if _PAGE:
            _PAGE.context.close()
        if _BROWSER:
            _BROWSER.close()
        if _PLAYWRIGHT:
            _PLAYWRIGHT.stop()
    except Exception:
        pass
    finally:
        _PAGE = None
        _BROWSER = None
        _PLAYWRIGHT = None


atexit.register(shutdown)


def fetch_html(
    url: str,
    wait_selectors: Iterable[str] = (),
    settle_ms: int = 500,
    pause: bool = True,
) -> str:
    """Fetch a page via real Chrome and return its HTML.

    ``wait_selectors`` are tried in order (best-effort) to let dynamic tables render;
    a miss is not fatal. Raises RuntimeError if the page comes back Akamai-blocked.

    A navigation timeout is retried up to ``FETCH_RETRIES`` times with growing backoff;
    the browser session is recycled before the final attempt in case the page/context is
    wedged. If every attempt times out the last error is re-raised for the caller to handle.
    """
    if pause:
        human_pause()
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            page = get_page()
            page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT * 1000)
            try:
                # Best-effort only: Akamai/analytics beacons keep the network busy, so a full
                # TIMEOUT-long wait would stall every page. wait_selectors below is the real gate.
                page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_MS)
            except PlaywrightTimeoutError:
                pass
            for sel in wait_selectors:
                try:
                    page.wait_for_selector(sel, timeout=TIMEOUT * 1000)
                    break
                except PlaywrightTimeoutError:
                    continue
            if settle_ms:
                page.wait_for_timeout(settle_ms)
            html = page.content()
        except PlaywrightTimeoutError:
            if attempt >= FETCH_RETRIES:
                log.warning("fetch %s: timed out after %d attempts; giving up", url, attempt)
                raise
            log.warning("fetch %s: timeout (attempt %d/%d), retrying", url, attempt, FETCH_RETRIES)
            # A hung page/context can persist across a goto; recycle the browser before the
            # final attempt so the retry starts from a clean session.
            if attempt == FETCH_RETRIES - 1:
                shutdown()
            time.sleep(RETRY_BACKOFF * attempt)
            continue
        if "Access Denied" in html and len(html) < 1000:
            raise RuntimeError(
                f"stats.ncaa.org returned Access Denied for {url} — is real Chrome "
                f"(channel={CHANNEL!r}) available?"
            )
        return html
