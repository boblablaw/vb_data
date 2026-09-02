"""Enrichment loaders: NCAA logos, player photos, RPI rankings, and AVCA poll.

- logos: refresh teams.logo_light/logo_dark from teams.json (paths under assets/logos/).
- photos: match files in assets/player_photos/ (named "<Team_slug>_<Player_slug>.jpg") to
  players and set players.photo_path.
- rpi: fetch the NCAA D1 WVB RPI table and set teams.rpi_rank / teams.rpi_record.
- avca: fetch the AVCA Coaches Poll (top 25) and set teams.avca_rank (NULL outside the poll).
"""
from __future__ import annotations

import re
from io import StringIO
from pathlib import Path

import pandas as pd
import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import REPO_ROOT
from ..log import get_logger
from ..models import Player, Team
from ..scrape.teams_json import load_teams as load_teams_json
from ..util import normalize_school_key
from .common import clean_str

log = get_logger(__name__)

PHOTOS_DIR = REPO_ROOT / "assets" / "player_photos"
RPI_URL = "https://www.ncaa.com/rankings/volleyball-women/d1/ncaa-womens-volleyball-rpi"
AVCA_URL = "https://www.ncaa.com/rankings/volleyball-women/d1/avca-rankings"


def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", s or "")
    return re.sub(r"_+", "_", s).strip("_")


def enrich_logos(session: Session, path: str | None = None) -> dict:
    by_name: dict[str, dict] = {}
    for e in load_teams_json(path):
        name = clean_str(e.get("team")) or clean_str(e.get("short_name"))
        if name:
            by_name[name] = e
    n = 0
    for team in session.scalars(select(Team)).all():
        e = by_name.get(team.name)
        if not e:
            continue
        team.logo_light = clean_str(e.get("ncaa_logo_light"))
        team.logo_dark = clean_str(e.get("ncaa_logo_dark"))
        n += 1
    session.flush()
    log.info("enrich_logos: %d teams updated", n)
    return {"teams": n}


def enrich_photos(session: Session, season: int, photos_dir: Path | None = None) -> dict:
    pdir = Path(photos_dir) if photos_dir else PHOTOS_DIR
    if not pdir.exists():
        raise FileNotFoundError(f"player photos dir not found: {pdir}")
    # Index available photos by (team_slug, player_slug).
    index: dict[tuple[str, str], str] = {}
    for f in pdir.iterdir():
        if not f.is_file() or f.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            continue
        stem = f.stem
        # Filenames are "<Team_slug>_<Player_slug>" — match greedily against known teams.
        index[stem.lower()] = str(f.relative_to(REPO_ROOT))

    matched = 0
    players = session.scalars(
        select(Player).where(Player.season == season)
    ).all()
    for p in players:
        team = p.team
        cands = [team.name, team.short_name] if team else []
        found = None
        for tname in cands:
            if not tname:
                continue
            key = f"{_slug(tname)}_{_slug(p.name)}".lower()
            if key in index:
                found = index[key]
                break
        if found:
            p.photo_path = found
            matched += 1
    session.flush()
    log.info("enrich_photos: %d/%d players matched to a photo (season %d)",
             matched, len(players), season)
    return {"matched": matched, "players": len(players)}


def _fetch_rankings_table(url: str, label: str) -> pd.DataFrame | None:
    try:
        resp = requests.get(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; vb-rankings/1.0)"}, timeout=30
        )
        resp.raise_for_status()
        tables = pd.read_html(StringIO(resp.text))
    except Exception as e:
        log.warning("%s fetch/parse failed: %s", label, e)
        return None
    for df in tables:
        cols = [str(c).strip().lower() for c in df.columns]
        if any("rank" in c for c in cols) and any(
            ("team" in c or "school" in c or "institution" in c) for c in cols
        ):
            return df
    return tables[0] if tables else None


def _fetch_rpi_table() -> pd.DataFrame | None:
    return _fetch_rankings_table(RPI_URL, "RPI")


def enrich_rpi(session: Session, csv_path: Path | None = None) -> dict:
    """Update teams.rpi_rank/rpi_record from the live NCAA table (or a CSV override)."""
    if csv_path:
        df = pd.read_csv(csv_path)
    else:
        df = _fetch_rpi_table()
    if df is None or df.empty:
        log.warning("enrich_rpi: no RPI data available")
        return {"teams": 0}

    cols = {str(c).strip().lower(): c for c in df.columns}
    rank_col = next((cols[c] for c in cols if "rank" in c), None)
    team_col = next((cols[c] for c in cols if "team" in c or "school" in c or "institution" in c), None)
    record_col = next((cols[c] for c in cols if "record" in c), None)
    if not team_col:
        log.warning("enrich_rpi: could not locate a team column")
        return {"teams": 0}

    lookup = {normalize_school_key(t.name): t for t in session.scalars(select(Team)).all()}
    # Also index aliases.
    for t in list(lookup.values()):
        for a in (t.aliases or []):
            lookup.setdefault(normalize_school_key(a), t)

    n = 0
    for _, r in df.iterrows():
        team = lookup.get(normalize_school_key(str(r[team_col])))
        if team is None:
            continue
        if rank_col is not None:
            try:
                team.rpi_rank = int(str(r[rank_col]).strip())
            except (ValueError, TypeError):
                team.rpi_rank = None
        if record_col is not None:
            team.rpi_record = clean_str(r[record_col])
        n += 1
    session.flush()
    log.info("enrich_rpi: %d teams updated", n)
    return {"teams": n}


# School cells in the AVCA table carry a first-place-vote suffix, e.g. "Nebraska (57)".
_VOTE_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")


def enrich_avca(session: Session, csv_path: Path | None = None) -> dict:
    """Set teams.avca_rank from the AVCA Coaches Poll (top 25); NULL for everyone else.

    Only 25 teams are ranked, so every team's prior rank is cleared first and the current poll's
    25 are set — this keeps the column reflecting *this week's* poll rather than an accumulation of
    stale ranks. Mirrors :func:`enrich_rpi` for team resolution (name + aliases)."""
    if csv_path:
        df = pd.read_csv(csv_path)
    else:
        df = _fetch_rankings_table(AVCA_URL, "AVCA")
    if df is None or df.empty:
        log.warning("enrich_avca: no AVCA data available")
        return {"teams": 0}

    cols = {str(c).strip().lower(): c for c in df.columns}
    rank_col = next((cols[c] for c in cols if "rank" in c and "previous" not in c), None)
    team_col = next(
        (cols[c] for c in cols if "team" in c or "school" in c or "institution" in c), None
    )
    if not team_col or rank_col is None:
        log.warning("enrich_avca: could not locate rank/team columns")
        return {"teams": 0}

    lookup = {normalize_school_key(t.name): t for t in session.scalars(select(Team)).all()}
    for t in list(lookup.values()):
        for a in (t.aliases or []):
            lookup.setdefault(normalize_school_key(a), t)

    # Clear stale ranks so the column reflects only the current poll.
    for t in session.scalars(select(Team).where(Team.avca_rank.is_not(None))).all():
        t.avca_rank = None

    n = 0
    for _, r in df.iterrows():
        name = _VOTE_SUFFIX.sub("", str(r[team_col]).strip())
        team = lookup.get(normalize_school_key(name))
        if team is None:
            log.warning("enrich_avca: unmatched poll team %r", name)
            continue
        try:
            team.avca_rank = int(str(r[rank_col]).strip())
        except (ValueError, TypeError):
            continue
        n += 1
    session.flush()
    log.info("enrich_avca: %d teams ranked", n)
    return {"teams": n}
