"""Deterministic prose generation for scouting reports (no LLM).

Two sections, each a list of paragraph strings:

* :func:`profile_prose` — a neutral read on who the team is and how they play.
* :func:`keys_prose` — "keys to beating them", turning weaknesses/insights into tactical advice.

Adjectives are keyed to percentile bands (percentiles are pre-oriented so high = better), which keeps
the sentences natural without generation. Everything hedges on small samples.
"""
from __future__ import annotations

# (min_pct, adjective) — first match wins; percentiles are oriented so higher is always better.
_BANDS = [
    (90, "elite"), (75, "strong"), (60, "above-average"), (40, "solid"),
    (25, "below-average"), (10, "weak"), (0, "poor"),
]


def band(pct: float | None) -> str:
    if pct is None:
        return "unremarkable"
    for lo, adj in _BANDS:
        if pct >= lo:
            return adj
    return "poor"


def _ordinal(n: int) -> str:
    """1 -> '1st', 2 -> '2nd', 3 -> '3rd', 11 -> '11th', etc."""
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def _ord(pct: float | None) -> str:
    """A percentile as an ordinal phrase, e.g. '78th percentile nationally'."""
    if pct is None:
        return ""
    return f"{_ordinal(round(pct))} percentile nationally"


def fmt_hit(v: float | None) -> str:
    """Hitting pct in volleyball style: .245, -.050."""
    if v is None:
        return "—"
    s = f"{abs(v):.3f}".lstrip("0")
    return f"-{s}" if v < 0 else s


def fmt_rate(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}"


def fmt_pctshare(v: float | None) -> str:
    return "—" if v is None else f"{round(v * 100)}%"


def _val(kind: str, v) -> str:
    if v is None:
        return "—"
    if kind == "pct3":
        return fmt_hit(v)
    if kind == "pctshare":
        return fmt_pctshare(v)
    if kind == "int":
        return str(int(v))
    return fmt_rate(v)


def _clean_name(name: str) -> str:
    """Drop a trailing alias parenthetical for prose readability — but only a multi-word one, so
    disambiguators like 'Miami (OH)' or 'Long Island University (LIU)' stay intact while noise like
    'University of Maryland Eastern Shore (UMES or Maryland Eastern Shore)' loses its tail."""
    i = name.rfind("(")
    if i > 0 and name.rstrip().endswith(")"):
        inner = name[i + 1:name.rstrip().rfind(")")]
        if " " in inner.strip():
            return name[:i].strip()
    return name


def _team_name(payload: dict) -> str:
    return _clean_name(payload.get("team") or "This team")


def _hitter_line(o: dict) -> str:
    """A secondary hitter as 'Name (POS, K.K/set)', dropping the position when it's unknown."""
    rate = fmt_rate(o.get("kills_per_set"))
    pos = o.get("position")
    if pos:
        return "{} ({}, {}/set)".format(o["name"], pos, rate)
    return "{} ({}/set)".format(o["name"], rate)


def profile_prose(payload: dict) -> list[str]:
    paras: list[str] = []
    rec = payload.get("record") or {}
    pct = payload.get("percentiles") or {}
    leaders = payload.get("leaders") or {}
    sample = payload.get("sample") or {}
    name = _team_name(payload)

    # P1 — identity: record, conference, streak. Note the conference record only when there are
    # conference games; otherwise show the conference as a parenthetical affiliation so an overall
    # record (e.g. 3-0 non-conference) can't read as a conference record.
    bits = []
    w, l = rec.get("wins"), rec.get("losses")
    if w is not None and l is not None:
        conf = rec.get("conference")
        cw, cl = rec.get("conf_wins"), rec.get("conf_losses")
        if conf and cw is not None and (cw + cl) > 0:
            bits.append(f"{name} is {w}-{l} this season, {cw}-{cl} in the {conf}")
        elif conf:
            bits.append(f"{name} ({conf}) is {w}-{l} this season")
        else:
            bits.append(f"{name} is {w}-{l} this season")
    streak = rec.get("win_streak")
    if streak and streak >= 3:
        bits.append(f"on a {streak}-match win streak")
    elif streak and streak <= -3:
        bits.append(f"having lost {abs(streak)} straight")
    p1 = ", ".join(bits) + "." if bits else f"{name}."
    # System (5-1 / 6-2).
    system = payload.get("system") or {}
    stype, setters = system.get("type"), system.get("setters") or []
    if stype == "5-1" and setters:
        p1 += f" They run a 5-1 with {setters[0]} running the offense."
        # Second-ball aggressiveness of the setter — flag an attacking setter, or note a pure
        # distributor, only at the extremes of the setter population.
        sa = system.get("attack") or {}
        aps, spct = sa.get("attacks_per_set"), sa.get("pct")
        if aps is not None and spct is not None:
            if spct >= 80:
                clause = (f" {setters[0]} is an aggressive second-ball threat, swinging "
                          f"{fmt_rate(aps)} times per set")
                if sa.get("hit_pct") is not None:
                    clause += f" and hitting {fmt_hit(sa['hit_pct'])}"
                p1 += clause + "."
            elif spct <= 40:
                p1 += (f" {setters[0]} is a traditional set-first setter who rarely attacks the "
                       f"second ball.")
    elif stype == "6-2" and len(setters) >= 2:
        p1 += f" They run a 6-2, splitting setting duties between {setters[0]} and {setters[1]}."
    elif stype == "6-2":
        p1 += " They run a 6-2 with two setters."
    paras.append(p1)

    # P2 — offense.
    hit = pct.get("hit_pct", {})
    kps = pct.get("kills_per_set", {})
    off_band = band(hit.get("pct") if hit else None)
    osent = f"Offensively they're {off_band}"
    extras = []
    if hit:
        extras.append(f"hitting {fmt_hit(hit['value'])} as a team ({_ord(hit['pct'])})")
    if kps:
        extras.append(f"{fmt_rate(kps['value'])} kills per set")
    if extras:
        osent += " — " + " and ".join(extras) + "."
    else:
        osent += "."
    hitters = leaders.get("hitters") or []
    if hitters:
        h = hitters[0]
        hp = h.get("nat_pct")
        tier = ""
        if hp is not None and hp >= 85:
            tier = ", an elite mark"
        elif hp is not None and hp >= 65:
            tier = ", a strong mark"
        pos = f" ({h['position']})" if h.get("position") else ""
        osent += (f" {h['name']}{pos} is the primary weapon at "
                  f"{fmt_rate(h.get('kills_per_set'))} kills/set on {fmt_hit(h.get('hit_pct'))}"
                  f"{tier}.")
        if len(hitters) > 1:
            others = ", ".join(_hitter_line(o) for o in hitters[1:3])
            osent += f" Secondary options: {others}."
    paras.append(osent)

    # Middle usage — only add the editorial tail at the extremes; in the middle band it would
    # contradict the "solid/above-average" descriptor, so just state the number.
    mid = pct.get("middle_share", {})
    if mid:
        pctv = mid["pct"]
        middles = leaders.get("middles") or []
        subj = "Their middles"
        if middles:
            subj += " (" + " and ".join(m["name"] for m in middles) + ")"
        sent = (f"{subj} take {fmt_pctshare(mid['value'])} of the team's swings "
                f"({band(pctv)} middle usage, {_ord(pctv)})")
        if pctv is not None and pctv >= 75:
            sent += " — the middle is a genuine focal point of their attack."
        elif pctv is not None and pctv <= 25:
            sent += " — a pin-dominant attack that leans on the outside hitters."
        else:
            sent += "."
        paras.append(sent)

    # P3 — defense & blocking.
    oh = pct.get("opp_hit_pct", {})
    bl = pct.get("blocks_per_set", {})
    dbits = []
    if oh:
        dbits.append(f"they hold opponents to {fmt_hit(oh['value'])} ({band(oh['pct'])} defense, "
                     f"{_ord(oh['pct'])})")
    if bl:
        dbits.append(f"block {fmt_rate(bl['value'])}/set ({band(bl['pct'])})")
    if dbits:
        paras.append("Defensively, " + " and ".join(dbits) + ".")

    # P4 — auto-surfaced strengths not already narrated. Exclude win %/set win % too: they're
    # résumé metrics already stated in the opening record line, and on a small early-season sample a
    # perfect 1.000 is trivially "elite" and crowds out genuinely novel standouts. Also exclude
    # opponent kills/set: the defense sentence already covers defense via opponent hit % + blocks,
    # and a high opp-kills percentile (few swings faced) reads as a "strength" that contradicts a
    # mediocre opponent-hit-% line.
    narrated = {"hit_pct", "kills_per_set", "opp_hit_pct", "blocks_per_set", "middle_share",
                "win_pct", "set_pct", "opp_kills_per_set"}
    insights = payload.get("insights") or {}
    extra_str = [s for s in insights.get("strengths", []) if s["key"] not in narrated]
    if extra_str:
        chunks = [f"{s['label']} ({_val(s['kind'], s['value'])}, {_ord(s['pct'])})"
                  for s in extra_str[:3]]
        paras.append("Also standing out: " + "; ".join(chunks) + ".")

    if sample.get("low_sample"):
        paras.append(f"Note: this is an early-season read on only {sample.get('matches')} "
                     f"matches — treat it as a first impression.")
    return paras


def keys_prose(payload: dict) -> list[str]:
    paras: list[str] = []
    name = _team_name(payload)
    pct = payload.get("percentiles") or {}
    insights = payload.get("insights") or {}
    rots = payload.get("rotations") or {}
    sample = payload.get("sample") or {}

    lead = f"How to beat {name}:"
    weaknesses = insights.get("weaknesses", [])
    notes = insights.get("notes", [])

    keys: list[str] = []

    # Marquee key: the weakest rotation (only when PBP exists). Only frame it as a genuine target
    # when the sideout% is actually soft; a strong team's "weakest" rotation can still be ~60%+,
    # where "target it, they side out just X%" overstates the opportunity.
    weakest = rots.get("weakest")
    if weakest and weakest.get("sideout_pct") is not None:
        so = weakest["sideout_pct"]
        if so < 55:
            keys.append(
                f"Target rotation R{weakest['rotation']} — they side out just {so}% there, their "
                f"most vulnerable rotation. Serve tough to hold them in it.")
        else:
            keys.append(
                f"Their softest receiving rotation is R{weakest['rotation']} ({so}% sideout) — the "
                f"best spot to press with your serve, though they hold up well across the board.")

    # Metric-driven keys.
    def has_weak(k):
        return any(w["key"] == k for w in weaknesses)

    oh = pct.get("opp_hit_pct", {})
    if oh and oh.get("pct") is not None and oh["pct"] <= 40:
        keys.append(f"You can score on them — they let opponents hit {fmt_hit(oh['value'])} "
                    f"({band(oh['pct'])} defense). Run your offense with confidence.")
    bl = pct.get("blocks_per_set", {})
    if bl and bl.get("pct") is not None and bl["pct"] <= 30:
        keys.append(f"Their block is modest ({fmt_rate(bl['value'])}/set) — swing over and around "
                    f"it, and your middles should find room.")
    he = pct.get("hit_errors_per_set", {})
    if he and he.get("pct") is not None and he["pct"] <= 25:
        keys.append("They give points away with hitting errors — keep balls in play and make them "
                    "beat you across a long rally.")
    # Contrast notes that are exploitable.
    for n in notes:
        if n.get("kind") == "weakness":
            keys.append(n["text"])

    # In/out-of-system.
    phase = payload.get("phase") or {}
    if phase.get("in_out_gap") is not None and phase["in_out_gap"] >= 0.080 and not any(
            "out of system" in k or "out of it" in k for k in keys):
        keys.append("Serve them off the net — they're notably sharper in system than out, so "
                    "first-contact pressure pays off.")

    if not keys:
        # A complete team: point at its least-strong comparable area.
        ranked = sorted((v for v in pct.values() if v.get("pct") is not None),
                        key=lambda d: d["pct"])
        if ranked:
            soft = ranked[0]
            keys.append(f"This is a well-rounded team with few obvious holes — their least "
                        f"dominant area is {soft['label']} ({_ord(soft['pct'])}), so that's where "
                        f"to apply pressure.")
        else:
            keys.append("Not enough data yet to identify clear weaknesses.")

    if sample.get("low_sample"):
        lead += " (Early sample — these reads may shift as the season develops.)"
    if not sample.get("has_pbp"):
        keys.append("Rotation-level detail isn't available for this team yet (no play-by-play), so "
                    "these keys are based on box-score trends.")

    paras.append(lead)
    paras.extend(keys)
    return paras
