"""Cross-check the hand-maintained ``teams.json`` conference field against the NCAA's own
per-season conference membership (the source of truth the rest of the pipeline already scrapes).

The diff is name-agnostic: NCAA and ``teams.json`` spell conferences differently
("The American" vs "American Conference", "NEC" vs "Northeast Conference"), so comparing name
strings directly would drown real moves in false positives. Instead we infer, per NCAA
conference, which ``teams.json`` conference its members *mostly* map to (the mode), then flag the
minority — that's exactly the realignment fingerprint (e.g. Colorado State labelled Mountain
West while its 8 new conference-mates are already Pac-12).

``diff_conferences`` is pure (no network) so it is unit-tested; ``verify_conferences`` wires it
to the live NCAA fetch.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from .scrape.team_list import fetch_conference_membership
from .scrape.teams_json import load_teams, team_id_for


def diff_conferences(
    json_teams: list[dict],
    ncaa_membership: dict[str, tuple[str, str]],
    year: int,
) -> dict:
    """Compare teams.json conferences to NCAA membership for ``year``. Returns a report dict.

    ``ncaa_membership``: ``{team_id -> (ncaa_conference_name, ncaa_team_name)}``.
    """
    json_by_tid: dict[str, dict] = {}
    for e in json_teams:
        tid = team_id_for(e, year)
        if tid:
            json_by_tid[tid] = e

    # Group NCAA team ids by their NCAA conference, then infer the corresponding teams.json
    # conference name for each (the mode of members' current json conferences).
    ncaa_by_conf: dict[str, list[str]] = defaultdict(list)
    for tid, (cname, _tname) in ncaa_membership.items():
        ncaa_by_conf[cname].append(tid)

    conf_map: dict[str, str] = {}
    for cname, tids in ncaa_by_conf.items():
        votes = Counter(
            (json_by_tid[t].get("conference") or "").strip()
            for t in tids
            if t in json_by_tid and (json_by_tid[t].get("conference") or "").strip()
        )
        if votes:
            conf_map[cname] = votes.most_common(1)[0][0]

    mismatches: list[dict] = []
    for tid, (cname, tname) in ncaa_membership.items():
        e = json_by_tid.get(tid)
        if e is None:
            continue  # reported under missing_in_json
        expected = conf_map.get(cname)
        actual = (e.get("conference") or "").strip()
        if expected and actual != expected:
            mismatches.append({
                "team_id": tid,
                "team": e.get("team"),
                "json_conference": actual,
                "ncaa_conference": cname,
                "expected_conference": expected,
            })

    missing_in_json = [
        {"team_id": tid, "ncaa_team_name": tname, "ncaa_conference": cname}
        for tid, (cname, tname) in ncaa_membership.items()
        if tid not in json_by_tid
    ]
    missing_in_ncaa = [
        {"team_id": tid, "team": e.get("team"), "json_conference": e.get("conference")}
        for tid, e in json_by_tid.items()
        if tid not in ncaa_membership
    ]

    mismatches.sort(key=lambda m: (m["ncaa_conference"], m["team"] or ""))
    missing_in_json.sort(key=lambda m: (m["ncaa_conference"], m["ncaa_team_name"] or ""))
    missing_in_ncaa.sort(key=lambda m: (m["json_conference"] or "", m["team"] or ""))
    return {
        "year": year,
        "counts": {
            "ncaa_teams": len(ncaa_membership),
            "json_teams_with_id": len(json_by_tid),
            "mismatches": len(mismatches),
            "missing_in_json": len(missing_in_json),
            "missing_in_ncaa": len(missing_in_ncaa),
        },
        "conf_name_map": dict(sorted(conf_map.items())),
        "mismatches": mismatches,
        "missing_in_json": missing_in_json,
        "missing_in_ncaa": missing_in_ncaa,
    }


def verify_conferences(year: int, division: int = 1) -> dict:
    """Fetch NCAA conference membership for ``year`` and diff it against teams.json."""
    ncaa = fetch_conference_membership(year, division)
    return diff_conferences(load_teams(), ncaa, year)
