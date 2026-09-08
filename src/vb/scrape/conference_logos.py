"""Download the curated set of NCAA D1 conference logos.

Unlike team logos (resolved live against henrygd's per-school API), conference logos have no single
programmatic source with usable coverage — ESPN's CDN tops out around half the conferences and omits
the marquee ones, and the ESPN APIs expose no conference logo at all. So the mapping is **curated**:
``data/conference_logos.json`` pins each conference name to a Wikipedia logo file + resolved URL. This
module downloads those URLs into the static asset dir and writes the served path back into the JSON.

Logos are committed to the repo (a fixed set of ~31), so production never depends on Wikipedia at
runtime; this command just (re)generates them. ``vb enrich conference-logos`` then copies the path
into ``conferences.logo``.

Files land in ``src/vb/api/static/assets/logos/conferences/<slug>.<ext>`` (served under /ui). The marks
are dark, full-color brand logos (no light/dark variants), so the UI renders them on a light chip.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

from ..config import REPO_ROOT
from ..log import get_logger

log = get_logger(__name__)

# Curated source-of-truth: {conference_name: {"file": "File:...", "url": "https://...", "logo": ...}}.
CONF_LOGOS_JSON = REPO_ROOT / "data" / "conference_logos.json"

STATIC_ROOT = REPO_ROOT / "src" / "vb" / "api" / "static"
LOGO_SUBDIR = Path("assets") / "logos" / "conferences"

# Wikipedia/Wikimedia asks for a descriptive UA; be polite between fetches. upload.wikimedia.org
# throttles automated bursts aggressively (HTTP 429), so pace slowly and back off on 429 — this job
# runs rarely (curated set, committed to the repo), so slow-and-polite is fine.
_UA = "vb_data-conference-logos/1.0 (personal NCAA volleyball stats project)"
_REQUEST_DELAY_SECONDS = 0.5
_MAX_RETRIES = 5
_MAX_BACKOFF_SECONDS = 30.0   # cap: the originals endpoint can send Retry-After: 600, don't honor it


def _fetch(session: requests.Session, url: str) -> bytes:
    """GET with exponential backoff on HTTP 429 (respecting Retry-After when present, capped)."""
    for attempt in range(_MAX_RETRIES):
        resp = session.get(url, timeout=30)
        if resp.status_code == 429 and attempt < _MAX_RETRIES - 1:
            wait = min(float(resp.headers.get("Retry-After") or 0) or 4.0 * (attempt + 1),
                       _MAX_BACKOFF_SECONDS)
            log.info("429 for %s; backing off %.0fs (attempt %d)", url, wait, attempt + 1)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        if len(resp.content) < 200:
            raise ValueError(f"suspiciously small payload ({len(resp.content)}B)")
        return resp.content
    resp.raise_for_status()  # exhausted retries on 429
    raise RuntimeError("unreachable")


def _slug(name: str) -> str:
    """Filesystem-safe lowercase slug for a conference name ("Big Ten Conference" -> big_ten)."""
    s = re.sub(r"\s+Conference$", "", name)          # drop the redundant trailing word
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")
    return s.lower()


# Width of the rendered thumbnail we fetch. The chip displays at ~24px, so 500px is retina-sharp.
_THUMB_WIDTH = 500

_ORIG_RE = re.compile(
    r"^(https://upload\.wikimedia\.org/wikipedia/[^/]+/)([0-9a-fA-F]/[0-9a-fA-F]{2}/)(.+)$"
)


def _thumb(url: str) -> tuple[str, str]:
    """Map an original upload.wikimedia.org URL to its cached thumbnail URL + served file ext.

    The originals endpoint throttles automated bursts hard (HTTP 429 with 10-min cool-downs); the
    thumbnail service is cache-served and reliable. SVGs render to PNG thumbnails; rasters keep their
    type. Returns (thumb_url, ext). Falls back to the original URL if the path is unexpected.
    """
    m = _ORIG_RE.match(url)
    fname = url.rsplit("/", 1)[-1]                    # last path segment (already URL-encoded)
    src_ext = (fname.rsplit(".", 1)[-1].lower() if "." in fname else "svg")
    is_vector = src_ext in ("svg", "svgz")
    ext = "png" if is_vector else src_ext
    if not m:
        return url, ext                              # unknown shape: fetch as-is
    prefix, hashdir, name = m.groups()
    thumb_name = f"{_THUMB_WIDTH}px-{name}" + (".png" if is_vector else "")
    return f"{prefix}thumb/{hashdir}{name}/{thumb_name}", ext


def load_conf_logos() -> dict[str, dict]:
    return json.loads(CONF_LOGOS_JSON.read_text(encoding="utf-8"))


def download_conference_logos(
    *,
    only: set[str] | None = None,
    force: bool = False,
    write_json: bool = True,
) -> dict:
    """Download each curated conference logo and record its served path in the JSON.

    Re-downloads a logo when its file is missing, its extension changed, or ``force``. Pass ``only``
    (a set of exact conference names) to restrict the run. Returns counts + any failures.
    """
    mapping = load_conf_logos()
    out_dir = STATIC_ROOT / LOGO_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers["User-Agent"] = _UA

    downloaded, skipped, failed, no_url = [], [], [], []
    for name, entry in mapping.items():
        if only and name not in only:
            continue
        url = entry.get("url")
        if not url:
            no_url.append(name)
            continue
        thumb_url, ext = _thumb(url)
        rel = LOGO_SUBDIR / f"{_slug(name)}.{ext}"
        entry["logo"] = str(rel)                       # served path, regardless of (re)download
        dest = STATIC_ROOT / rel
        if dest.exists() and not force:
            skipped.append(name)
            continue
        try:
            content = _fetch(session, thumb_url)
        except (requests.RequestException, ValueError) as e:
            log.warning("conference logo fetch failed name=%s url=%s: %s", name, url, e)
            failed.append((name, str(e)))
            time.sleep(_REQUEST_DELAY_SECONDS)
            continue
        dest.write_bytes(content)
        downloaded.append(name)
        log.info("conference logo %s -> %s", name, rel)
        time.sleep(_REQUEST_DELAY_SECONDS)

    if write_json:
        CONF_LOGOS_JSON.write_text(
            json.dumps(mapping, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    log.info(
        "download_conference_logos: %d downloaded, %d skipped, %d failed, %d no-url",
        len(downloaded), len(skipped), len(failed), len(no_url),
    )
    return {"downloaded": downloaded, "skipped": skipped, "failed": failed, "no_url": no_url}
