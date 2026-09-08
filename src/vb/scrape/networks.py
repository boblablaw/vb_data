"""Normalize messy broadcast/channel strings from every feed into one canonical network.

Every source — conference ICS platform hosts, TPS M3U channel labels, XMLTV EPG channel names —
routes its raw string through :func:`normalize`, which returns ``(label, logo_key)`` or ``None`` to
DROP the listing. ``logo_key`` names an SVG under ``static/assets/logos/networks/<key>.svg`` (the
frontend falls back to a text pill when the asset is missing), and is ``None`` for networks we show
as text only.

Design notes:
* Order matters — the first matching rule wins, so specific patterns precede generic ones
  ("CBS Sports Network" before bare "CBS", "ESPN+" before "ESPN").
* National broadcast games arrive as hundreds of *local affiliates* ("AL | Birmingham | FOX 6
  WBRC"); :func:`_affiliate_network` folds those to the parent net before the rules run.
* Disney+ is dropped entirely (it's a simultaneous ESPN+/ACC feed, not its own broadcast).
"""
from __future__ import annotations

import re

# --- local-affiliate folding (ported from volleyball_digest render.py) --------------------------
# Broadcast networks whose local affiliates fold into one label. Cable nets that merely contain
# these words ("FOX Sports", "CBS Sports Network") lack a call sign / geo tag, so they're left alone.
_BROADCAST_NETS = [
    ("THE CW", "CW"), ("CW", "CW"), ("FOX", "FOX"), ("ABC", "ABC"),
    ("NBC", "NBC"), ("CBS", "CBS"),
]
_CALLSIGN_RE = re.compile(r"\b[KW][A-Z]{2,3}(?:-[A-Z0-9]+)?\b")   # WBRC, KTTV, KDFX-CA
_STATE_CITY_RE = re.compile(r"^[A-Z]{2}\s*\|")                    # "AL | Birmingham | ..."


def _affiliate_network(ch: str) -> str | None:
    """If ``ch`` is a local broadcast affiliate, return its parent network label, else None."""
    up = ch.upper()
    for kw, label in _BROADCAST_NETS:
        if not re.search(rf"\b{re.escape(kw)}\d*\b", up):
            continue
        local = (_CALLSIGN_RE.search(ch) or _STATE_CITY_RE.match(ch)
                 or re.search(rf"\b{re.escape(kw)}\s?\d{{1,2}}\b", up))
        if local:
            return label
    return None


# --- drop rules (never show these) --------------------------------------------------------------
_DROP = (
    "disney+", "disney +", "disney plus",     # simultaneous ESPN+/ACC feed, per product decision
    "radio", ".sxm", "siriusxm", "sirius xm",
)

# --- canonical network rules --------------------------------------------------------------------
# (compiled matcher against the lowercased raw string, canonical label, logo_key | None).
# Order is significant: specific first.
def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


_RULES: list[tuple[re.Pattern, str, str | None]] = [
    # ESPN family (ESPN+ / ESPNU / ESPN2 before bare ESPN)
    (_rx(r"\bespn\s*news\b"), "ESPNews", None),
    (_rx(r"\bespn\s*u\b|espnu"), "ESPNU", "espnu"),
    (_rx(r"\bespn\s*2\b|espn2"), "ESPN2", "espn2"),
    (_rx(r"espn\s*\+|espn\s*plus|espnplus|watchespn"), "ESPN+", "espn-plus"),
    # "ESPN1" is just linear ESPN (no such channel as ESPN1); fold it before the generic rules.
    (_rx(r"\bespn\s*1\b|espn1"), "ESPN", "espn"),
    # ICS "Streaming Video" links point at the generic ESPN app player (espn.com), which can't
    # distinguish linear ESPN from ESPN+ — label it honestly. TPS channels named "ESPN" lack ".com".
    (_rx(r"espn\.com"), "ESPN/ESPN+", "espn"),
    (_rx(r"\bespn\b"), "ESPN", "espn"),
    # ACC / SEC / Big Ten (extras + streaming before the linear net)
    (_rx(r"acc\s*n\s*x|acc\s*network\s*extra|accnx"), "ACC Network Extra", "accnx"),
    (_rx(r"acc\s*network|accn\b"), "ACC Network", "accn"),
    (_rx(r"sec\s*network\s*\+|sec\s*\+|secplus|sec\s*network\s*extra"), "SEC Network+", "secn-plus"),
    (_rx(r"sec\s*network|secn\b"), "SEC Network", "secn"),
    (_rx(r"big\s*ten\s*plus|big10\+|b1g\+|btn\s*\+|bigtenplus"), "B1G+", "b1g-plus"),
    (_rx(r"big\s*ten\s*network|\bbtn\b"), "Big Ten Network", "btn"),
    (_rx(r"\bb1g\b"), "Big Ten Network", "btn"),
    # FOX family
    (_rx(r"\bfs1\b|fox\s*sports\s*1"), "FS1", "fs1"),
    (_rx(r"\bfs2\b|fox\s*sports\s*2"), "FS2", "fs2"),
    (_rx(r"fox\s*one|fox\s*1\b"), "FOX One", None),
    (_rx(r"fox\s*sports"), "FOX Sports", None),
    (_rx(r"\bfox\b"), "FOX", "fox"),
    # Other national
    (_rx(r"peacock"), "Peacock", "peacock"),
    (_rx(r"paramount\s*\+|paramount\s*plus"), "Paramount+", None),
    (_rx(r"\babc\b"), "ABC", "abc"),
    (_rx(r"cbs\s*sports\s*network|cbssn"), "CBS Sports Network", None),
    (_rx(r"\bcbs\b"), "CBS", "cbs"),
    (_rx(r"\bnbc\b"), "NBC", "nbc"),
    # Conference streaming platforms
    (_rx(r"pac\s*-?\s*12"), "Pac-12 Network", "pac12"),
    (_rx(r"mountain\s*west|watch\.themw|themw\.com|\bmw\+"), "MW+", "mw-plus"),
    (_rx(r"flo\s*sports|flo\s*volleyball|flo\s*college|flocollege|flosports"), "FloSports", "flosports"),
    (_rx(r"big\s*west"), "Big West", None),
    (_rx(r"midco"), "Midco Sports Plus", None),
    (_rx(r"nec\s*front\s*row"), "NEC Front Row", None),
    (_rx(r"swac\s*tv|tv\.swac"), "SWAC TV", None),
    (_rx(r"patriot\s*league"), "Patriot League Network", None),
    (_rx(r"meac"), "MEAC Sports Network", None),
    (_rx(r"summit"), "Summit League Network", None),
    (_rx(r"\bvbtv\b|volleyball\s*tv"), "VolleyballTV", None),
    (_rx(r"byu\s*tv|byutv"), "BYUtv", None),
    (_rx(r"longhorn\s*network|\blhn\b"), "Longhorn Network", None),
    (_rx(r"youtube|@pac12"), "YouTube", "youtube"),
]


# A bare hostname (no spaces/scheme) like "uconnhuskies.com" or "csura.ms" — the shape a school's
# own webstream takes once _stream_host() has reduced an ICS link to its host. TPS channel names
# ("ESPN+ 45: A vs B") carry spaces/colons, so they never match this.
_WEBSTREAM_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$")
_WEBSTREAM_JUNK = ("urldefense.",)   # Proofpoint email-security URL wrapper, not a real stream host


def normalize(raw: str | None) -> tuple[str, str | None] | None:
    """Map a raw feed channel/platform string to ``(label, logo_key)``, or ``None`` to drop it."""
    if not raw:
        return None
    s = raw.strip()
    low = s.lower()
    if any(bad in low for bad in _DROP):
        return None
    aff = _affiliate_network(s)
    if aff:
        # Re-run the parent label through the rules so it gets its canonical form + logo_key.
        for rx, label, key in _RULES:
            if rx.search(aff):
                return label, key
        return aff, None
    for rx, label, key in _RULES:
        if rx.search(s):
            return label, key
    # Fallback: a bare school/conference webstream host (e.g. "uconnhuskies.com") that matched no
    # known network. ICS "Streaming Video" links for these point at the school's own free stream —
    # a real broadcast, just not a national network. Surface it generically rather than dropping the
    # game's only listing. (Product decision: label it, don't try to identify the platform.)
    if _WEBSTREAM_HOST_RE.match(low) and not any(j in low for j in _WEBSTREAM_JUNK):
        return "Web stream", None
    return None
