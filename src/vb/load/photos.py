"""Fresh per-season player headshots: scrape each school's roster page, match to our ``Player``
rows, download the image into the served static assets dir, and set ``Player.photo_path``.

Files are keyed by the stable **``ncaa_player_id``** (not name), so a transfer's photo follows the
person across schools/seasons and re-runs overwrite in place. This retires the old name-slug reuse
in :func:`vb.load.enrichment.enrich_photos` (stale across seasons — wrong team, wrong photo).
"""
from __future__ import annotations

import re

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..log import get_logger
from ..models import Player, Team
from ..scrape.photos import UA, fetch_team_photos
from ..util import canonical_name

log = get_logger(__name__)

# Photos live in a dedicated dir (settings.photos_dir), NOT in the packaged static tree — the host
# scraper writes here and the read-only container serves it via a bind mount at /ui/assets/
# player_photos (see vb.api.main). photo_path stays this stable URL-relative value regardless of
# where the files physically sit, so it works identically in local dev and prod.
PHOTO_REL_PREFIX = "assets/player_photos"
_CONTENT_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/pjpeg": ".jpg",
    "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
}


def _ext_for(url: str, content_type: str | None) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _CONTENT_EXT:
        return _CONTENT_EXT[ct]
    m = re.search(r"\.(jpe?g|png|webp|gif)(?:$|\?)", url, re.IGNORECASE)
    if m:
        return "." + m.group(1).lower().replace("jpeg", "jpg")
    return ".jpg"


def _download(client: httpx.Client, url: str, stem: str) -> str:
    """Download ``url`` into the static photos dir as ``<stem>.<ext>``; return the static-relative
    path. Removes any prior file for this stem (extension may change) so re-runs stay clean."""
    r = client.get(url)
    r.raise_for_status()
    if not r.content:
        raise httpx.HTTPError("empty image body")
    ext = _ext_for(str(r.url), r.headers.get("content-type"))
    photos_dir = settings.photos_dir
    photos_dir.mkdir(parents=True, exist_ok=True)
    for old in photos_dir.glob(f"{stem}.*"):
        old.unlink()
    (photos_dir / f"{stem}{ext}").write_bytes(r.content)
    return f"{PHOTO_REL_PREFIX}/{stem}{ext}"


def scrape_player_photos(
    session: Session,
    season: int,
    *,
    only_teams: list[str] | None = None,
    timeout: float = 20.0,
    og_fallback: bool = True,
) -> dict:
    """Scrape 2026-style fresh headshots for every team's ``season`` roster.

    Matches each scraped hit to a ``Player`` by **name first** (order-independent token set, robust
    to "First Last" vs "Last, First"), then by **unambiguous jersey number** as a backup. Downloads
    matched images to ``static/assets/player_photos/<ncaa_player_id>.<ext>`` and sets
    ``Player.photo_path``. ``only_teams`` restricts to teams whose name/short_name matches (case-
    insensitive) — handy for smoke-testing one SIDEARM + one non-SIDEARM school.
    """
    want = {t.lower() for t in only_teams} if only_teams else None
    teams = session.scalars(select(Team)).all()
    if want is not None:
        teams = [
            t for t in teams
            if (t.name and t.name.lower() in want) or (t.short_name and t.short_name.lower() in want)
        ]

    matched = downloaded = teams_done = total_players = 0
    with httpx.Client(
        headers={"User-Agent": UA}, timeout=timeout, follow_redirects=True
    ) as client:
        for team in teams:
            url = team.website
            if not url:
                continue
            players = session.scalars(
                select(Player).where(Player.team_id == team.id, Player.season == season)
            ).all()
            if not players:
                continue
            total_players += len(players)
            label = team.short_name or team.name

            try:
                hits = fetch_team_photos(url, client=client, og_fallback=og_fallback)
            except httpx.HTTPError as e:
                log.warning("photos: %s roster fetch failed (%s): %s", label, url, e)
                continue

            by_name = {canonical_name(p.name): p for p in players}
            by_jersey: dict[int, list[Player]] = {}
            for p in players:
                if p.number is not None:
                    by_jersey.setdefault(p.number, []).append(p)

            t_matched = 0
            for h in hits:
                if not h.image_url:
                    continue
                p = by_name.get(canonical_name(h.name))
                if p is None and h.jersey is not None:
                    cand = by_jersey.get(h.jersey)
                    if cand and len(cand) == 1:
                        p = cand[0]
                if p is None:
                    continue
                matched += 1
                t_matched += 1
                stem = p.ncaa_player_id or f"id{p.id}"
                try:
                    p.photo_path = _download(client, h.image_url, stem)
                    downloaded += 1
                except httpx.HTTPError as e:
                    log.debug("photos: download failed for %s (%s): %s", p.name, h.image_url, e)

            # Commit per team (not once at the very end) so freshly-scraped headshots appear in the
            # UI incrementally as each roster finishes, and a mid-run failure keeps completed teams
            # rather than rolling back the whole sweep.
            session.commit()
            teams_done += 1
            log.info("photos: %s -> %d/%d players matched", label, t_matched, len(players))

    log.info(
        "scrape_player_photos: %d teams, %d/%d players matched, %d downloaded (season %d)",
        teams_done, matched, total_players, downloaded, season,
    )
    return {
        "teams": teams_done, "matched": matched,
        "downloaded": downloaded, "players": total_players,
    }
