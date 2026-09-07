"""Fetch + parse broadcast listings from every source into a common ``FeedBroadcast`` shape.

Three sources, ported/trimmed from the ``volleyball_digest`` project:

* **Conference ICS calendars** (``fetch_ics``) — public SIDEARM subscription feeds, one HTTP call
  per conference. PRIMARY source; carries the streaming platform in a ``Streaming Video:`` line.
* **TPS M3U playlist** (``fetch_playlist``) — dedicated event channels; the matchup is in the
  channel NAME, the network in the group-title. FALLBACK; skipped without TPS creds.
* **TPS XMLTV EPG** (``fetch_epg``) — 24/7 channels' programme guide; the matchup is in the
  programme title, the network in the channel name. FALLBACK; skipped without TPS creds.

Each yields ``FeedBroadcast(team_a, team_b, date, network_raw, source, is_live, start_utc)`` with the
two RAW team strings (the loader resolves them to our team ids) and the RAW network string (the
loader canonicalizes it via :mod:`vb.scrape.networks`). This module never touches the DB and never
canonicalizes — it just extracts. "Schedule-first": we don't classify leagues here; a listing that
doesn't resolve to a real D1 team pair on a real date is dropped downstream.
"""
from __future__ import annotations

import concurrent.futures as _cf
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from ..log import get_logger

log = get_logger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
_ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class FeedBroadcast:
    """One airing from any feed, with RAW (un-normalized) team + network strings."""
    team_a: str | None
    team_b: str | None
    date: str | None           # ISO YYYY-MM-DD (local ET), or None if the feed gave no time
    network_raw: str           # raw channel/platform string -> vb.scrape.networks.normalize()
    source: str                # 'ics' | 'playlist' | 'epg'
    is_live: bool = True
    start_utc: datetime | None = None
    channel_no: str | None = None   # TPS event-feed slot ("45" from "ESPN+ 45: ..."), playlist only


# --- name / matchup extraction (ported from volleyball_digest teams.py) -------------------------
_MARKER_RE = re.compile(r"[ʰ-˿ᴀ-ᶿ⁰-₟]+")           # superscript/modifier runs (ᴸᶦᵛᵉ, ᴺᵉʷ)
_DOW = r"(?:Sun|Mon|Tue|Wed|Thu|Fri|Sat)"
_NETWORK_HINTS = {
    "fs1", "fs2", "fox", "espn", "espnu", "espn2", "btn", "big ten network",
    "peacock", "accn", "accnx", "sec network", "secn", "secplus", "sec+",
    "cbssn", "espn+", "espn plus", "disney+", "b1g+", "big10+", "flosports",
    "paramount+", "vbtv",
}
_PREFIXES = [
    r"^(?:BIG10\+|Flo Sports|FOX ONE|NFHS Network|VBTV|ESPN\+|Disney\+ Events|MiLB|FS1|FS2|Peacock)\s*\d+:\s*",
    r"^[A-Za-z0-9+.'&|/ ]{1,28}?\s\d{1,3}:\s*",     # generic "<channel label> NN: "
    r"^flovolleyball:\s*\d{4}\s*",
    r"^Volleyball\s*\((?:W|M)\)\s*",
    r"^(?:Women's|Men's) College Volleyball\s*:?\s*",
    r"^NCAA (?:Women's|Men's) Volleyball\s*:?\s*",
    r"^B1G (?:Women's|Men's) Volleyball\s*-\s*",
    r"^Players Era Volleyball Showcase\s*-\s*",
    r"^Showcase\s*[-–]\s*",
    r"^College Volleyball\s*:?\s*",
    r"^Beach Volleyball\s*:?\s*",
    r"^Volleyball\s*[:\-]\s*",
]
_SEPARATORS = [" vs. ", " vs ", " v. ", " at ", " @ ", " — ", " – ", " - "]
_OTHER_SPORT_RE = re.compile(
    r"\b(football|basketball|soccer|hockey|baseball|softball|lacrosse|tennis|"
    r"wrestl|gymnast|golf|swim|track|rugby|cricket|press conference)\b", re.IGNORECASE)


def _strip_markers(s: str) -> str:
    return _MARKER_RE.sub("", s).replace("🏐", "").strip()


def has_live_marker(s: str | None) -> bool:
    """EPG tags the LIVE airing with a superscript "ᴸᶦᵛᵉ" run; replays lack it. NFKD folds it to
    "Lɪve"; fold + normalize the small-cap ɪ, then look for "live" in the non-ASCII chars only."""
    if not s:
        return False
    marker_src = "".join(c for c in s if ord(c) > 127)
    folded = unicodedata.normalize("NFKD", marker_src).lower().replace("ɪ", "i")
    return "live" in folded


def _strip_trailing(t: str) -> str:
    t = re.sub(r"\s*@.*$", "", t)
    t = re.sub(rf"\s+{_DOW}\b\.?$", "", t)
    t = re.sub(r"\s*[-–]\s*(?:Women's|Men's).*$", "", t)
    m = re.search(r"\s*\(([^()]*)\)\s*$", t)          # trailing broadcaster paren, keep (OH)/(CA)
    if m:
        inner = m.group(1).strip()
        if " " in inner or re.search(r"\b(vs|at)\b", inner) or inner.lower() in _NETWORK_HINTS:
            t = t[: m.start()].rstrip()
    return t.strip(" .-–")


def _clean_title(title: str) -> str:
    t = _strip_markers(title)
    for _ in range(6):                                # prefixes nest; loop until stable
        for pat in _PREFIXES:
            new = re.sub(pat, "", t, count=1)
            if new != t:
                t = new
                break
        else:
            break
    return _strip_trailing(t)


def extract_matchup(title: str) -> tuple[str | None, str | None]:
    """Return (team_a, team_b) from a broadcast/programme title, or (None, None)."""
    cleaned = _clean_title(title)
    for sep in _SEPARATORS:
        if sep in cleaned:
            a, b = cleaned.split(sep, 1)
            a, b = a.strip(" .-–"), b.strip(" .-–")
            if a and b:
                return a, b
    return None, None


def _looks_volleyball(text: str) -> bool:
    low = text.lower()
    return "volleyball" in low or "vbtv" in low


# --- conference ICS calendars (ported from volleyball_digest confwatch.py) ----------------------
# conferenceSeo slug -> (host, women's-volleyball sport_id). sport_ids from <host>/calendar.aspx.
_ICS_FEEDS: dict[str, tuple[str, int]] = {
    "acc": ("theacc.com", 27),
    "america-east": ("americaeast.com", 14),
    "big-east": ("bigeast.com", 22),
    "big-sky": ("bigskyconf.com", 14),
    "big-south": ("bigsouthsports.com", 20),
    "big-west": ("bigwest.org", 17),
    "caa": ("caasports.com", 17),
    "horizon": ("horizonleague.org", 23),
    "ivy-league": ("ivyleague.com", 32),
    "mac": ("mac-sports.com", 15),
    "meac": ("meacsports.com", 18),
    "metro": ("maacsports.com", 22),
    "mountain-west": ("themw.com", 18),
    "mvc": ("mvc-sports.com", 13),
    "nec": ("northeastconference.org", 255),
    "pac-12": ("pac-12.com", 30),
    "patriot": ("patriotleague.com", 23),
    "southland": ("southland.org", 19),
    "summit-league": ("thesummitleague.org", 18),
    "swac": ("swac.org", 19),
    "wcc": ("wccsports.com", 19),
}
_SUMMARY_PREFIX = re.compile(r"^(?:women's|men's)?\s*(?:college\s+)?volleyball\s*[:\-–]?\s*", re.IGNORECASE)
_STREAM_RE = re.compile(r"Streaming Video:\s*(\S+)")
_DTSTART_RE = re.compile(r"^DTSTART[^:]*:(.*)$", re.MULTILINE)
_SUMMARY_RE = re.compile(r"^SUMMARY[^:]*:(.*)$", re.MULTILINE)


def _stream_host(url: str) -> str:
    """First host from a (possibly glued) Streaming Video URL — the feed concatenates the real link
    with the calendar link, so read only up to the next scheme/slash."""
    m = re.match(r"https?://([^/\s]+?)(?=https?://|[/\s]|$)", url.strip())
    return m.group(1).lower() if m else ""


def _ics_teams(summary: str) -> tuple[str | None, str | None]:
    s = re.sub(r"\s+", " ", _SUMMARY_PREFIX.sub("", summary)).strip()
    for sep in (" vs. ", " vs ", " at ", " @ "):
        if sep in s:
            a, b = s.split(sep, 1)
            a, b = a.strip(), b.strip()
            if a and b:
                return a, b
    return None, None


def _ics_dtstart(raw: str) -> datetime | None:
    raw = raw.strip()
    try:
        if raw.endswith("Z") and "T" in raw:
            return datetime.strptime(raw[:15], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
        if len(raw) >= 8 and raw[:8].isdigit():
            return datetime.strptime(raw[:8], "%Y%m%d")
    except ValueError:
        pass
    return None


def _parse_ics(text: str) -> list[FeedBroadcast]:
    text = re.sub(r"\r?\n[ \t]", "", text)            # unfold ICS continuation lines
    out: list[FeedBroadcast] = []
    for ev in text.split("BEGIN:VEVENT")[1:]:
        sv = _STREAM_RE.search(ev)
        if not sv:
            continue                                   # no streaming platform -> nothing to tag
        host = _stream_host(sv.group(1))
        sm = _SUMMARY_RE.search(ev)
        dm = _DTSTART_RE.search(ev)
        if not host or not sm or not dm:
            continue
        a, b = _ics_teams(sm.group(1))
        if not a or not b:
            continue
        dt = _ics_dtstart(dm.group(1))
        start_utc = dt if (dt and dt.tzinfo) else None
        day = (dt.astimezone(_ET) if dt.tzinfo else dt).date().isoformat() if dt else None
        out.append(FeedBroadcast(team_a=a, team_b=b, date=day, network_raw=host,
                                 source="ics", is_live=True, start_utc=start_utc))
    return out


def _load_ics(conf: str, timeout: int = 20) -> str | None:
    host, sid = _ICS_FEEDS[conf]
    url = (f"https://{host}/services/responsive-calendar-subscription.ashx/"
           f"calendar.ics?sport_id={sid}")
    try:
        r = requests.get(url, headers=_UA, timeout=timeout)
        r.raise_for_status()
        if "BEGIN:VEVENT" in r.text:
            return r.text
    except Exception as e:
        log.warning("ICS feed %s failed: %s", conf, e)
    return None


def fetch_ics(conferences: set[str] | None = None) -> list[FeedBroadcast]:
    """All streaming-platform listings from the conference ICS calendars (all feeds by default)."""
    todo = [c for c in (conferences or _ICS_FEEDS) if c in _ICS_FEEDS]
    out: list[FeedBroadcast] = []
    with _cf.ThreadPoolExecutor(max_workers=min(8, max(1, len(todo)))) as ex:
        for text in ex.map(_load_ics, todo):
            if text:
                out.extend(_parse_ics(text))
    log.info("ics: %d streaming listings from %d conference feed(s)", len(out), len(todo))
    return out


# --- TPS M3U playlist ---------------------------------------------------------------------------
_EXTINF_RE = re.compile(r'#EXTINF:.*?group-title="([^"]*)".*?,(.*)$')
_NAME_RE = re.compile(r'tvg-name="([^"]*)"')
_REGION_PREFIX_RE = re.compile(r"^[A-Za-z]{2,3}\s*[:|]\s*")   # "USA | ", "US: "
_CHANNEL_SLOT_RE = re.compile(r"^(.*\S)\s+\d{1,3}$")           # "BIG10+ 02" -> "BIG10+"
_SLOT_NUM_RE = re.compile(r"^.+?\s(\d{1,3})\s*:")             # "ESPN+ 45: A vs B" -> "45"
_PAREN_NET_RE = re.compile(r"\(([A-Za-z0-9+ .'/-]{2,20})\)")  # "... (ACCNX)" -> "ACCNX"
_REPLAY_RE = re.compile(r"\b(replay|rerun|re-air|encore|delayed|tape)\b", re.IGNORECASE)


def _playlist_channel_no(name: str) -> str | None:
    """The event-feed slot from a playlist name ("ESPN+ 45: ..." -> "45"), or None."""
    m = _SLOT_NUM_RE.match(name)
    return m.group(1) if m else None


def _playlist_network_raw(name: str, group: str) -> str:
    """Best network string for a playlist row. A combined multiplex channel ("SEC+ / ACC extra")
    names the real sub-net in parens ("... (ACCNX)") — prefer that so an ACCNX game isn't labeled
    SEC Network+. Otherwise prefer the (region-stripped) group-title, else the name's leading label
    (before the first ':' or trailing slot number)."""
    for cand in _PAREN_NET_RE.findall(name):
        if cand.strip().lower() in _NETWORK_HINTS:
            return cand.strip()
    g = _REGION_PREFIX_RE.sub("", group).strip()
    if g:
        return g
    label = name.split(":", 1)[0].strip()
    slot = _CHANNEL_SLOT_RE.match(label)
    return (slot.group(1) if slot else label).strip()


def parse_playlist(text: str) -> list[FeedBroadcast]:
    out: list[FeedBroadcast] = []
    for line in text.splitlines():
        if not line.startswith("#EXTINF"):
            continue
        m = _EXTINF_RE.match(line)
        if not m:
            continue
        group, disp = m.group(1), m.group(2).strip()
        nm = _NAME_RE.search(line)
        name = (nm.group(1) if nm else disp).strip()
        # No "volleyball" keyword gate here: dedicated event channels read "BIG10+ 02: A vs B".
        # The other-sport guard + downstream schedule pair-match (must be a real D1 WVB pair on a
        # real date) are the filter. EPG, which lists ALL programming, keeps the keyword gate below.
        if _OTHER_SPORT_RE.search(name):
            continue
        a, b = extract_matchup(name)
        if not a or not b:
            continue
        out.append(FeedBroadcast(
            team_a=a, team_b=b, date=None,
            network_raw=_playlist_network_raw(name, group),
            source="playlist", is_live=not bool(_REPLAY_RE.search(name)),
            channel_no=_playlist_channel_no(name),
        ))
    return out


def fetch_playlist(url: str, timeout: int = 180) -> list[FeedBroadcast]:
    try:
        r = requests.get(url, headers=_UA, timeout=timeout)
        r.raise_for_status()
    except Exception as e:
        log.warning("TPS playlist fetch failed: %s", e)
        return []
    out = parse_playlist(r.text)
    log.info("playlist: %d volleyball listings", len(out))
    return out


# --- TPS XMLTV EPG ------------------------------------------------------------------------------
def _epg_time(raw: str) -> datetime | None:
    """Parse an XMLTV start like '20260830153000 -0400' to a tz-aware datetime."""
    raw = (raw or "").strip()
    m = re.match(r"(\d{14})\s*([+-]\d{4})?", raw)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    if m.group(2):
        sign = 1 if m.group(2)[0] == "+" else -1
        offh, offm = int(m.group(2)[1:3]), int(m.group(2)[3:5])
        dt = dt.replace(tzinfo=timezone(timedelta(minutes=sign * (offh * 60 + offm))))
    return dt


def parse_epg(text: bytes | str) -> list[FeedBroadcast]:
    out: list[FeedBroadcast] = []
    channels: dict[str, str] = {}
    root = ET.fromstring(text)
    for elem in root:
        if elem.tag == "channel":
            cid = elem.get("id")
            dn = elem.findtext("display-name") or cid
            if cid:
                channels[cid] = dn or ""
        elif elem.tag == "programme":
            title = (elem.findtext("title") or "").strip()
            if not _looks_volleyball(title) or _OTHER_SPORT_RE.search(title):
                continue
            chan = channels.get(elem.get("channel"), elem.get("channel") or "")
            a, b = extract_matchup(title)
            if not a or not b:
                continue
            start = _epg_time(elem.get("start", ""))
            day = start.astimezone(_ET).date().isoformat() if start else None
            out.append(FeedBroadcast(
                team_a=a, team_b=b, date=day, network_raw=chan,
                source="epg", is_live=has_live_marker(title),
                start_utc=start.astimezone(UTC) if start else None,
            ))
    return out


def fetch_epg(url: str, timeout: int = 180) -> list[FeedBroadcast]:
    try:
        r = requests.get(url, headers=_UA, timeout=timeout)
        r.raise_for_status()
    except Exception as e:
        log.warning("TPS EPG fetch failed: %s", e)
        return []
    try:
        out = parse_epg(r.content)
    except ET.ParseError as e:
        log.warning("TPS EPG parse failed: %s", e)
        return []
    log.info("epg: %d volleyball listings", len(out))
    return out
