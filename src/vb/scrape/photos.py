"""Scrape fresh player headshots from each school's official roster page.

Players transfer and schools shoot new media-day photos every season, so we re-scrape from the
*current* roster page rather than reusing prior-year images (last year's name-keyed dump is stale —
wrong team, wrong photo). Most schools run **SIDEARM**, whose roster HTML carries per-player cards
with a headshot ``<img>`` (lazy-loaded via ``data-src`` / ``srcset``, not ``src``) and a
``/roster/<slug>/<id>`` detail link. A minority are on newer platforms where the roster image is
JS-rendered; for those we fall back to each player detail page's ``og:image``.

The pure parsers (:func:`parse_roster`, :func:`og_image_from_html`) are separated from the HTTP
orchestration (:func:`fetch_team_photos`) so they can be unit-tested against small HTML fixtures.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from ..log import get_logger

log = get_logger(__name__)

# A recent desktop Safari UA — school CDNs serve the real (non-placeholder) markup to it.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")

# A player's detail link, in either convention we've seen:
#   SIDEARM (classic):   /roster/<name-slug>/<numeric-id>
#   WMT / Nuxt (newer):  /sports/<sport>/roster/player/<name-slug>
_PLAYER_HREF_RE = re.compile(r"/roster/(?:player/[^/?#]+|[^/?#]+/\d+)/?(?:[?#]|$)")
# URLs that are clearly not a headshot: site chrome, sponsor/site logos, placeholders, spacers.
_SKIP_IMG_RE = re.compile(
    r"(?:\.svg(?:$|\?)|/logos?/|placeholder|no[-_]?photo|spacer|blank\.|1x1\.|missing)", re.IGNORECASE
)
# SIDEARM's image resizer. Roster cards point <img> at it with width=100&height=100, i.e. a tiny
# square crop that guillotines the top of the head and leaves mostly neck. The full-resolution
# original sits untouched in its `url=` query param, so we rewrite the crop to a large, top-anchored
# portrait — the whole head is captured and our round CSS crop frames the face.
_SIDEARM_RESIZER_HOSTS = {"images.sidearmdev.com", "images.sidearmsports.com"}


def _upgrade_photo_url(url: str) -> str:
    """If ``url`` is a SIDEARM resizer crop, enlarge it to a top-anchored 3:4 portrait; else pass
    through unchanged (WMT/imgproxy and direct-CDN URLs are already full images)."""
    parts = urlsplit(url)
    if parts.netloc.lower() not in _SIDEARM_RESIZER_HOSTS:
        return url
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    if "url" not in q:  # not the wrapping form we know how to rewrite
        return url
    q["width"], q["height"] = "480", "640"   # 3:4 portrait: full head + shoulders
    q["gravity"] = "north"                    # anchor at the top so the top of the head is never cut
    q.setdefault("type", "webp")             # small, sharp; keeps any existing explicit type
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


@dataclass
class PhotoHit:
    """One player found on a roster page."""
    name: str
    jersey: int | None
    image_url: str | None       # best headshot URL on the roster card (None -> try og:image)
    player_url: str | None      # the player's detail page (for the og:image fallback)


def _best_img_url(img, base_url: str) -> str | None:
    """Pick the highest-quality real image URL from an ``<img>``.

    SIDEARM lazy-loads: the visible ``src`` is a placeholder while the real headshot sits in
    ``data-src`` / ``data-srcset`` / ``srcset``. Prefer those; from a ``srcset`` take the last
    (largest) candidate. Skip inline data-URIs and obvious non-headshots.
    """
    cand = img.get("data-src") or img.get("data-lazy-src") or img.get("data-original")
    if not cand:
        srcset = img.get("data-srcset") or img.get("srcset")
        if srcset:
            parts = [p.strip().split(" ")[0] for p in srcset.split(",") if p.strip()]
            cand = parts[-1] if parts else None
    if not cand:
        cand = img.get("src")
    if not cand:
        return None
    cand = cand.strip()
    if not cand or cand.startswith("data:"):
        return None
    absu = urljoin(base_url, cand)
    if _SKIP_IMG_RE.search(absu):
        return None
    return _upgrade_photo_url(absu)


def _jersey_from(text: str) -> int | None:
    m = re.search(r"#\s*(\d{1,2})\b", text or "")
    return int(m.group(1)) if m else None


def parse_roster(html: str, base_url: str) -> list[PhotoHit]:
    """Extract one :class:`PhotoHit` per player from a roster page.

    Anchors to each player's ``/roster/<slug>/<id>`` detail link (present on both SIDEARM and the
    newer platforms), then walks up into the surrounding card to find a headshot ``<img>`` and a
    jersey number. Players with an image-only anchor (no text) are still registered from the first
    *named* anchor for the same URL, so the og:image fallback can fill them.
    """
    soup = BeautifulSoup(html, "lxml")
    by_url: dict[str, PhotoHit] = {}
    order: list[str] = []
    for a in soup.find_all("a", href=True):
        if not _PLAYER_HREF_RE.search(a["href"]):
            continue
        player_url = urljoin(base_url, a["href"])
        name = " ".join((a.get_text() or "").split()).strip()
        # Skip anchors with no usable name (image-only / number-only links) — a sibling text anchor
        # for the same player carries the name. Names never contain digits.
        if not name or len(name) < 3 or any(ch.isdigit() for ch in name):
            by_url.setdefault(player_url, PhotoHit(name="", jersey=None, image_url=None,
                                                   player_url=player_url))
            if player_url not in order:
                order.append(player_url)
            continue
        hit = by_url.get(player_url)
        if hit is None:
            hit = PhotoHit(name=name, jersey=None, image_url=None, player_url=player_url)
            by_url[player_url] = hit
            order.append(player_url)
        elif not hit.name:
            hit.name = name
        # Walk up a few levels into the card, filling image + jersey from the first that has them.
        # (WMT cards nest the headshot a little deeper than SIDEARM, hence 5.)
        node = a
        for _ in range(5):
            node = node.parent
            if node is None:
                break
            if hit.image_url is None:
                img = node.find("img")
                if img is not None:
                    hit.image_url = _best_img_url(img, base_url)
            if hit.jersey is None:
                hit.jersey = _jersey_from(node.get_text(" "))
            if hit.image_url is not None and hit.jersey is not None:
                break
    return [by_url[u] for u in order if by_url[u].name]


def og_image_from_html(html: str, base_url: str) -> str | None:
    """The page's Open Graph / Twitter card image — on a *player* detail page this is the headshot."""
    soup = BeautifulSoup(html, "lxml")
    for prop in ("og:image", "twitter:image", "twitter:image:src"):
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        content = tag.get("content") if tag else None
        if content and content.strip():
            u = urljoin(base_url, content.strip())
            if not _SKIP_IMG_RE.search(u):
                return _upgrade_photo_url(u)
    return None


def _render_roster(url: str) -> str | None:
    """Render a JS-heavy roster page with real Chrome (last resort). Lazy-imports the Playwright
    fetch so the httpx path (and tests) don't depend on a browser; returns None on any failure."""
    try:
        from ..fetch import fetch_html
    except Exception:  # pragma: no cover - Playwright/browsers not installed
        return None
    try:
        # scroll=True forces WMT/Nuxt lazy-loaders to swap every off-screen card's placeholder for
        # its real headshot (without it, only the in-viewport few load and the rest share one image).
        return fetch_html(
            url, wait_selectors=("a[href*='/roster/']", "img"), settle_ms=2500, scroll=True
        )
    except Exception as e:  # pragma: no cover - network/render failure
        log.warning("photos: render fallback failed for %s: %s", url, e)
        return None


def fetch_team_photos(
    roster_url: str, *, client: httpx.Client, og_fallback: bool = True, max_og: int = 60,
    render_fallback: bool = True,
) -> list[PhotoHit]:
    """Fetch one team's roster page and return per-player :class:`PhotoHit`\\ s.

    Plain HTTP handles the ~90% of schools on classic SIDEARM. The newer WMT/Nuxt sites render their
    roster client-side, so static HTML yields no headshots — when ``render_fallback`` is on and the
    static parse finds *zero* images, re-render the page with real Chrome and re-parse. Any remaining
    imageless players (small SIDEARM stragglers) get their headshot from the detail-page ``og:image``,
    bounded by ``max_og`` to keep the job light.
    """
    r = client.get(roster_url)
    r.raise_for_status()
    hits = parse_roster(r.text, str(r.url))
    if render_fallback and not any(h.image_url for h in hits):
        rendered = _render_roster(roster_url)
        if rendered:
            r_hits = parse_roster(rendered, roster_url)
            if any(h.image_url for h in r_hits):
                return r_hits  # rendered DOM already carries every headshot; no og pass needed
    if og_fallback:
        need = [h for h in hits if not h.image_url and h.player_url]
        for h in need[:max_og]:
            try:
                pr = client.get(h.player_url)
                pr.raise_for_status()
                h.image_url = og_image_from_html(pr.text, str(pr.url))
            except httpx.HTTPError as e:
                log.debug("photos: og fallback failed for %s: %s", h.player_url, e)
    return hits
