"""Download NCAA team logos (light + dark SVG) from henrygd's NCAA API.

Ported from vb_scraper's ``download_ncaa_logos.py`` and **fixed**: the original matcher stripped
the token ``state`` when normalizing school names, so "X State" and "University of X"/"Ohio
University" collapsed to the same key and were assigned the *same* NCAA slug — every such pair
ended up sharing one (wrong) logo (e.g. Ohio State served Ohio's mark). The fix resolves each
team against henrygd's ``/schools-index`` by its **full name** (the index ``long`` field, which
spells out "State University"), keeping every token, so "The Ohio State University"→``ohio-st``
and "Ohio University"→``ohio`` resolve distinctly.

Logos are written to ``src/vb/api/static/assets/logos/ncaa/<Team_Name_Sanitized>_{light,dark}.svg``
(served statically) and the resolved ``ncaa_slug`` / ``ncaa_logo_light`` / ``ncaa_logo_dark`` are
written back into ``data/teams.json`` (the source of truth ``enrich_logos`` copies into the DB).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

from ..config import REPO_ROOT, settings
from ..log import get_logger

log = get_logger(__name__)

BASE_URL = "https://ncaa-api.henrygd.me"
# 0.25s between calls => max ~4 req/s, under henrygd's 5 req/s limit.
REQUEST_DELAY_SECONDS = 0.25
# Logos are served from the API's static dir; teams.json stores the "assets/..." suffix only.
STATIC_ROOT = REPO_ROOT / "src" / "vb" / "api" / "static"
LOGO_SUBDIR = Path("assets") / "logos" / "ncaa"


# Teams whose /schools-index slug has no logo, but whose logo is hosted under a different (usually
# pre-rebrand) slug. Keyed by exact team name → the slug that actually serves a logo SVG.
_SLUG_OVERRIDES = {
    "East Texas A&M University": "tex-am-commerce",  # index says east-tex-am (404); logo is Commerce's
}


def _norm(s: str | None) -> str:
    """Punctuation-insensitive, token-preserving normalization (keeps 'state', 'university', …)."""
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _safe_name(team_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", team_name).strip("_")


def fetch_schools_index() -> list[dict]:
    resp = requests.get(f"{BASE_URL}/schools-index", timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise TypeError(f"/schools-index returned {type(data).__name__}, expected list")
    return data


def build_lookups(index: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    """Return (by_long, by_name): normalized full-name→slug and short-name→slug maps.

    Full name (``long``) is primary because it disambiguates the "State" pairs; ``name`` (which
    abbreviates "St.") is a fallback for teams whose full name carries extra qualifiers.
    """
    by_long: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for e in index:
        slug = e.get("slug")
        if not slug:
            continue
        by_long.setdefault(_norm(e.get("long")), slug)
        by_name.setdefault(_norm(e.get("name")), slug)
    by_long.pop("", None)
    by_name.pop("", None)
    return by_long, by_name


def _strip_parens(s: str) -> str:
    """Drop trailing parentheticals/brackets, e.g. 'University of Mississippi (Ole Miss)[n]'."""
    return re.sub(r"\s*[(\[].*", "", s or "").strip()


def resolve_slug(team: dict, by_long: dict[str, str], by_name: dict[str, str]) -> str | None:
    """Best NCAA slug for a team, matching full name first, then short name / aliases."""
    name = team.get("team") or ""
    if override := _SLUG_OVERRIDES.get(name):
        return override
    if slug := by_long.get(_norm(name)):
        return slug
    # Many of our names carry trailing "(Ole Miss)" / "(UIC)[n]" tags the index full name omits.
    if (bare := _strip_parens(name)) != name and (slug := by_long.get(_norm(bare))):
        return slug
    for alias in [team.get("short_name"), *(team.get("team_name_aliases") or [])]:
        if not alias:
            continue
        if slug := (by_long.get(_norm(alias)) or by_name.get(_norm(alias))):
            return slug
    return by_name.get(_norm(name))


def _download(slug: str, dark: bool, session: requests.Session) -> bytes | None:
    url = f"{BASE_URL}/logo/{slug}.svg" + ("?dark=true" if dark else "")
    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("logo fetch failed slug=%s dark=%s: %s", slug, dark, e)
        return None
    return resp.content


def download_logos(
    teams_path: str | Path | None = None,
    *,
    only: set[str] | None = None,
    force: bool = False,
    write_json: bool = True,
) -> dict:
    """Resolve slugs and download logos.

    Re-resolves every team's slug against the live index and rewrites ``ncaa_slug`` in
    ``teams.json`` (fixing any stale/collided value). Logos are (re)downloaded for teams whose slug
    changed, whose file is missing, or all teams when ``force``. Pass ``only`` (a set of exact team
    names) to restrict the whole operation to specific teams.
    """
    path = Path(teams_path) if teams_path else settings.teams_json_path
    teams = json.loads(path.read_text(encoding="utf-8"))
    out_dir = STATIC_ROOT / LOGO_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)

    by_long, by_name = build_lookups(fetch_schools_index())
    session = requests.Session()
    session.headers["User-Agent"] = "vb_data-logo-sourcer/1.0"

    changed_slugs, downloaded, unresolved, failed = [], [], [], []
    for team in teams:
        name = team.get("team")
        if not name or (only and name not in only):
            continue
        slug = resolve_slug(team, by_long, by_name)
        if not slug:
            unresolved.append(name)
            continue
        old_slug = team.get("ncaa_slug")
        slug_changed = slug != old_slug
        if slug_changed:
            changed_slugs.append((name, old_slug, slug))
            team["ncaa_slug"] = slug

        safe = _safe_name(name)
        rel_light = LOGO_SUBDIR / f"{safe}_light.svg"
        rel_dark = LOGO_SUBDIR / f"{safe}_dark.svg"
        team["ncaa_logo_light"] = str(rel_light)
        team["ncaa_logo_dark"] = str(rel_dark)

        need = force or slug_changed or not (STATIC_ROOT / rel_light).exists() \
            or not (STATIC_ROOT / rel_dark).exists()
        if not need:
            continue
        got_any = False
        for dark, rel in ((False, rel_light), (True, rel_dark)):
            content = _download(slug, dark, session)
            time.sleep(REQUEST_DELAY_SECONDS)
            if content is None:
                failed.append((name, slug, "dark" if dark else "light"))
                continue
            (STATIC_ROOT / rel).write_bytes(content)
            got_any = True
        if got_any:
            downloaded.append(name)
            log.info("logo %s -> slug=%s", name, slug)

    if write_json and (changed_slugs or downloaded):
        # Preserve teams.json formatting (2-space indent, unicode, trailing newline).
        path.write_text(json.dumps(teams, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    log.info(
        "download_logos: %d slug fixes, %d downloaded, %d unresolved, %d failed",
        len(changed_slugs), len(downloaded), len(unresolved), len(failed),
    )
    return {
        "slug_changes": changed_slugs,
        "downloaded": downloaded,
        "unresolved": unresolved,
        "failed": failed,
    }
