"""Deterministic per-team scouting reports (no LLM).

The weekly ``vb build-scouting`` job composes existing aggregations into one report per team:

* :mod:`vb.scouting.metrics` — team offense/defense/résumé values + league percentiles (net-new
  cross-team comparison layer; nothing else in the repo computes percentiles across teams).
* :mod:`vb.scouting.pbp_rollup` — season-per-team play-by-play rollup (per-rotation sideout/hold/±,
  setter system, in/out-of-system hitting), built by looping the season's contests.
* :mod:`vb.scouting.insights` — outlier engine: surfaces extreme-percentile strengths/weaknesses
  (including ones not explicitly narrated) and telling cross-metric contrasts.
* :mod:`vb.scouting.prose` — deterministic sentence templates → a neutral team profile and a
  "keys to beating them" section.
* :mod:`vb.scouting.builder` — orchestrates the above and upserts ``scouting_reports`` rows.
"""
from .builder import build_scouting

__all__ = ["build_scouting"]
