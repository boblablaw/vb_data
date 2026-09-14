"""Outlier engine — surfaces what's notable about a team without it being hard-coded.

Given a team's percentile map (from :func:`vb.scouting.metrics.attach_percentiles`), it flags every
catalog metric that lands in an extreme band (a strength or an exploitable weakness) and detects a
few telling cross-metric *contrasts*. This is what lets the report keep finding things the user
didn't explicitly ask to track — any metric in the catalog that spikes gets surfaced.
"""
from __future__ import annotations

from .metrics import catalog_spec

STRONG_PCT = 85.0   # >= this percentile (oriented so high = better) is a strength
WEAK_PCT = 15.0     # <= this is an exploitable weakness
_MAX_PER_LIST = 6


def build_insights(pct_map: dict[str, dict], phase: dict | None = None) -> dict:
    """Return ``{strengths, weaknesses, notes}``.

    ``strengths``/``weaknesses`` are ranked lists of ``{key, label, pct, value, kind}``; ``notes``
    are short cross-metric observations (``{text, kind}``). ``pct_map`` maps metric key -> the
    per-metric dict produced by ``attach_percentiles``.
    """
    strengths, weaknesses = [], []
    for key, d in pct_map.items():
        pct = d.get("pct")
        if pct is None:
            continue
        spec = catalog_spec(key)
        if spec is not None and spec.get("auto", True) is False:
            continue  # bespoke-prose-only metric — keep it out of the generic lists
        entry = {"key": key, "label": d["label"], "pct": pct, "value": d["value"],
                 "kind": d["kind"]}
        if pct >= STRONG_PCT:
            strengths.append(entry)
        elif pct <= WEAK_PCT:
            weaknesses.append(entry)
    strengths.sort(key=lambda e: -e["pct"])
    weaknesses.sort(key=lambda e: e["pct"])

    notes = _contrasts(pct_map, phase)
    return {
        "strengths": strengths[:_MAX_PER_LIST],
        "weaknesses": weaknesses[:_MAX_PER_LIST],
        "notes": notes,
    }


def _p(pct_map: dict, key: str):
    d = pct_map.get(key)
    return d.get("pct") if d else None


def _contrasts(pct_map: dict, phase: dict | None) -> list[dict]:
    """Detect telling combinations across metrics. Each rule is guarded on data presence."""
    notes: list[dict] = []
    hit, kps = _p(pct_map, "hit_pct"), _p(pct_map, "kills_per_set")
    opp_hit = _p(pct_map, "opp_hit_pct")  # oriented: high pct = good defense
    blocks = _p(pct_map, "blocks_per_set")
    aces = _p(pct_map, "aces_per_set")
    win = _p(pct_map, "win_pct")
    sos = _p(pct_map, "sos")

    off_strong = max(v for v in (hit, kps) if v is not None) if (hit or kps) else None
    if off_strong is not None and off_strong >= 80 and opp_hit is not None and opp_hit <= 30:
        notes.append({"kind": "weakness",
                      "text": "Big offense but a leaky defense — they can be outscored in a track "
                              "meet rather than out-defended."})
    if blocks is not None and blocks >= 80 and hit is not None and hit <= 35:
        notes.append({"kind": "neutral",
                      "text": "They block well but don't hit efficiently — a defense-first team "
                              "you can stay with if you protect the ball."})
    if aces is not None and aces >= 85:
        notes.append({"kind": "weakness",
                      "text": "Serving is a real weapon for them — clean first-contact passing is a "
                              "must to blunt their runs."})
    if win is not None and win >= 80 and sos is not None and sos <= 25:
        notes.append({"kind": "neutral",
                      "text": "Their record is strong but built on a soft schedule — the win total "
                              "may overstate them."})
    if phase and phase.get("in_out_gap") is not None and phase["in_out_gap"] >= 0.100:
        notes.append({"kind": "weakness",
                      "text": "They're much sharper in system than out of it — serving them off the "
                              "net drops their attack markedly."})
    return notes
