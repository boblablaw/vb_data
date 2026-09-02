"""Backfill ``ncaa_team_ids[<year>]`` in data/teams.json from a harvested NCAA team list.

Matches each teams.json entry to the harvested list (see team_list.py) by normalized school
name/alias. A few NCAA names differ from ours by branding; those aliases are added to the
entry so the match is generic and persists. Dry-run by default; refuses to write below
``min_match`` matches (guards against a bad/partial harvest wiping ids).

Year semantics: ``year`` is the fall/season year — the same key used in ncaa_team_ids.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from ..config import settings
from ..log import get_logger
from ..util import normalize_school_key

log = get_logger(__name__)

# teams.json "team" -> NCAA list alias to add so the normalized match succeeds.
ALIAS_FIXES = {
    "University of Arkansas at Pine Bluff": "Ark.-Pine Bluff",
    "University of New Orleans": "LSU New Orleans",
}


def _load_team_list(path: Path) -> dict[str, tuple[str, str]]:
    """normalized name -> (team_id, raw name) from the harvested CSV."""
    out: dict[str, tuple[str, str]] = {}
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = normalize_school_key(row["team_name"])
            if key:
                out.setdefault(key, (row["team_id"], row["team_name"]))
    return out


def _entry_alias_keys(entry: dict) -> set[str]:
    cands = [entry.get("team"), entry.get("short_name")] + (entry.get("team_name_aliases") or [])
    return {normalize_school_key(c) for c in cands if c}


def backfill_team_ids(
    year: int,
    team_list_path: Path | None = None,
    teams_json_path: Path | None = None,
    min_match: int = 340,
    write: bool = False,
) -> dict:
    """Match harvested ids into teams.json[<year>]. Returns a summary dict.

    Only matched entries are touched. To re-point an existing year at a different season,
    strip the old ``"<year>"`` keys first — unmatched entries keep whatever id they had.
    """
    y = str(year)
    list_path = Path(team_list_path) if team_list_path else (
        settings.staging_dir / f"ncaa_wvb_team_list_{year}.csv"
    )
    if not list_path.exists():
        raise FileNotFoundError(
            f"team-list CSV not found: {list_path} (run `vb scrape team-list --year {year}`)"
        )
    tj_path = Path(teams_json_path) if teams_json_path else settings.teams_json_path

    name_to_id = _load_team_list(list_path)
    log.info("loaded %d teams from %s", len(name_to_id), list_path)

    teams = json.loads(tj_path.read_text(encoding="utf-8"))
    matched: list[str] = []
    unmatched: list[str] = []
    used_ids: set[str] = set()

    for entry in teams:
        # Apply known alias fixes first so they match and persist.
        fix = ALIAS_FIXES.get(entry.get("team"))
        if fix:
            aliases = entry.setdefault("team_name_aliases", [])
            if fix not in aliases:
                aliases.append(fix)

        hit = None
        for key in _entry_alias_keys(entry):
            if key in name_to_id:
                hit = name_to_id[key]
                break

        if hit:
            entry.setdefault("ncaa_team_ids", {})[y] = hit[0]
            matched.append(entry.get("team"))
            used_ids.add(hit[0])
        else:
            unmatched.append(entry.get("team"))

    unmapped = [(tid, nm) for _, (tid, nm) in name_to_id.items() if tid not in used_ids]

    log.info("matched %d/%d teams to a %s id", len(matched), len(teams), y)
    if len(matched) < min_match:
        raise SystemExit(
            f"only {len(matched)} matched (< {min_match}); refusing to write."
        )

    if write:
        tj_path.write_text(
            json.dumps(teams, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        log.info("wrote %s", tj_path)

    return {
        "year": year,
        "matched": len(matched),
        "total": len(teams),
        "unmatched": unmatched,
        "unmapped": [{"team_id": t, "name": n} for t, n in unmapped],
        "written": write,
    }


def _list_id_to_name(path: Path) -> dict[str, str]:
    """team_id -> NCAA display (short) name from the harvested team-list CSV."""
    out: dict[str, str] = {}
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tid = str(row["team_id"]).strip()
            name = (row["team_name"] or "").strip()
            if tid and name:
                out[tid] = name
    return out


def backfill_short_names(
    year: int,
    team_list_path: Path | None = None,
    teams_json_path: Path | None = None,
    min_match: int = 340,
    write: bool = False,
) -> dict:
    """Set ``short_name`` in teams.json from the NCAA team-list display names.

    The ``inst_team_list`` page renders NCAA's canonical short names (e.g. "Fresno St.",
    "Boise St."). Matches each teams.json entry to that list by its own ``ncaa_team_ids[<year>]``
    (exact id match — no fuzzy name matching), then overwrites ``short_name`` with the NCAA name.
    Dry-run by default; refuses to write below ``min_match`` id matches (guards a bad harvest).
    """
    y = str(year)
    list_path = Path(team_list_path) if team_list_path else (
        settings.staging_dir / f"ncaa_wvb_team_list_{year}.csv"
    )
    if not list_path.exists():
        raise FileNotFoundError(
            f"team-list CSV not found: {list_path} (run `vb scrape team-list --year {year}`)"
        )
    tj_path = Path(teams_json_path) if teams_json_path else settings.teams_json_path

    id_to_name = _list_id_to_name(list_path)
    log.info("loaded %d team names from %s", len(id_to_name), list_path)

    teams = json.loads(tj_path.read_text(encoding="utf-8"))
    matched: list[str] = []
    unmatched: list[str] = []
    changed: list[dict] = []

    for entry in teams:
        tid = (entry.get("ncaa_team_ids") or {}).get(y)
        ncaa_name = id_to_name.get(str(tid)) if tid else None
        if not ncaa_name:
            unmatched.append(entry.get("team"))
            continue
        matched.append(entry.get("team"))
        old = entry.get("short_name")
        if old != ncaa_name:
            changed.append({"team": entry.get("team"), "old": old, "new": ncaa_name})
            entry["short_name"] = ncaa_name

    log.info("matched %d/%d teams by %s id; %d short_names changed",
             len(matched), len(teams), y, len(changed))
    if len(matched) < min_match:
        raise SystemExit(
            f"only {len(matched)} matched (< {min_match}); refusing to write."
        )

    if write:
        tj_path.write_text(
            json.dumps(teams, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        log.info("wrote %s", tj_path)

    return {
        "year": year,
        "matched": len(matched),
        "total": len(teams),
        "changed": changed,
        "unmatched": unmatched,
        "written": write,
    }
