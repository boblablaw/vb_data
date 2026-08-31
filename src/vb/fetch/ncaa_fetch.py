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
import random
import time
from collections.abc import Iterable

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

# --------- tunable config (overridable by callers before first fetch) ---------
HEADLESS = True
CHANNEL = "chrome"                 # real Chrome; falls back to bundled chromium if absent
USER_AGENT = DEFAULT_UA
HUMAN_DELAY_RANGE = (3.0, 6.0)     # conservative jitter before each page fetch
TIMEOUT = 45                       # seconds per navigation
NETWORKIDLE_MS = 6000              # best-effort settle budget (analytics beacons never idle)

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
    try:
        _BROWSER = _PLAYWRIGHT.chromium.launch(headless=HEADLESS, channel=CHANNEL)
    except Exception as e:
        print(
            f"[WARN] Could not launch Chrome channel {CHANNEL!r} ({e}); falling back to "
            "bundled Chromium — stats.ncaa.org will likely return 'Access Denied'."
        )
        _BROWSER = _PLAYWRIGHT.chromium.launch(headless=HEADLESS)
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
    """
    if pause:
        human_pause()
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
    if "Access Denied" in html and len(html) < 1000:
        raise RuntimeError(
            f"stats.ncaa.org returned Access Denied for {url} — is real Chrome "
            f"(channel={CHANNEL!r}) available?"
        )
    return html
