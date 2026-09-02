"""One-off: backfill the 2026 preseason ranking snapshot (as_of 2026-08-06).

AVCA ranks come from the Aug 6 2025 AVCA/Taraflex DI preseason poll (valid until the first
in-season poll on 2026-08-31). RPI ranks/records are copied from the current teams table — early
RPI is last year's rollover, so it's valid before the season started.

Run on the box:  python - commit   < this file
Default (no arg) is a dry run that only reports team matches.
"""
import sys
from datetime import date

from sqlalchemy import select

from vb.db import session_scope
from vb.models import RankingSnapshot, Team
from vb.util import normalize_school_key

SEASON = 2026
AS_OF = "2026-08-06"

# Aug 6 preseason AVCA poll (rank -> school as printed on the poll graphic).
AVCA_POLL = [
    (1, "Nebraska"), (2, "Penn State"), (3, "Pittsburgh"), (4, "Louisville"), (5, "Texas"),
    (6, "Stanford"), (7, "Kentucky"), (8, "Wisconsin"), (9, "Texas A&M"), (10, "SMU"),
    (11, "Minnesota"), (12, "Creighton"), (13, "Arizona State"), (14, "Kansas"), (15, "Purdue"),
    (16, "Florida"), (17, "Missouri"), (18, "UCLA"), (19, "BYU"), (20, "Baylor"),
    (21, "USC"), (22, "Georgia Tech"), (23, "Utah"), (24, "Dayton"), (25, "TCU"),
]


def main():
    commit = len(sys.argv) > 1 and sys.argv[1] == "commit"
    day = date.fromisoformat(AS_OF)

    with session_scope() as s:
        teams = list(s.scalars(select(Team)).all())
        lookup = {normalize_school_key(t.name): t for t in teams}
        for t in teams:
            for a in (t.aliases or []):
                lookup.setdefault(normalize_school_key(a), t)

        # Resolve the poll names -> team ids.
        avca_by_team = {}
        unmatched = []
        for rank, name in AVCA_POLL:
            t = lookup.get(normalize_school_key(name))
            if t is None:
                unmatched.append((rank, name))
            else:
                avca_by_team[t.id] = rank

        print(f"AVCA matched {len(avca_by_team)}/25")
        for rank, name in AVCA_POLL:
            t = lookup.get(normalize_school_key(name))
            print(f"  #{rank:>2} {name:<15} -> {t.name if t else '*** UNMATCHED ***'}")
        if unmatched:
            print(f"!! {len(unmatched)} unmatched — fix aliases before committing:", unmatched)
            if commit:
                sys.exit("refusing to commit with unmatched poll teams")

        # RPI-ranked teams (current values = last year's rollover).
        rpi_teams = [t for t in teams if t.rpi_rank is not None]
        print(f"RPI-ranked teams (current rollover): {len(rpi_teams)}")

        if not commit:
            print("\nDRY RUN — no rows written. Re-run with 'commit' to write.")
            return

        # Idempotent upsert for this as_of: one row per team that has AVCA and/or RPI.
        existing = {
            r.team_id: r
            for r in s.scalars(
                select(RankingSnapshot).where(
                    RankingSnapshot.season == SEASON, RankingSnapshot.as_of == day
                )
            ).all()
        }
        by_id = {t.id: t for t in teams}
        team_ids = set(avca_by_team) | {t.id for t in rpi_teams}
        n = 0
        for tid in team_ids:
            t = by_id[tid]
            row = existing.get(tid)
            if row is None:
                row = RankingSnapshot(season=SEASON, as_of=day, team_id=tid)
                s.add(row)
            row.avca_rank = avca_by_team.get(tid)
            row.rpi_rank = t.rpi_rank
            row.rpi_record = t.rpi_record
            n += 1
        s.flush()
        print(f"wrote {n} preseason snapshot rows for {AS_OF}")


if __name__ == "__main__":
    main()
