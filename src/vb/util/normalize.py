"""Name / school / height / position normalizers.

Ported from the original vb_scraper ``scripts/helpers/utils.py`` (minus the requests-based
fetch_html and Excel helpers, which are not needed in the DB-first design).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

# stats.ncaa.org renders contest dates as "MM/DD/YYYY HH:MM AM/PM" (time sometimes absent).
_NCAA_DATETIME_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{4})(?:\s+(\d{1,2}:\d{2}\s*[AP]M))?", re.IGNORECASE
)


def parse_ncaa_datetime(raw: Any) -> str | None:
    """Parse an NCAA contest date/time into sortable ISO 'YYYY-MM-DD HH:MM' (date-only ok).

    Returns None if no date is found. The time is dropped only when the page omits it.
    """
    s = normalize_text(raw)
    if not s:
        return None
    m = _NCAA_DATETIME_RE.search(s)
    if not m:
        return None
    date_part, time_part = m.group(1), m.group(2)
    if time_part:
        try:
            dt = datetime.strptime(f"{date_part} {time_part.upper()}", "%m/%d/%Y %I:%M %p")
            return dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    try:
        return datetime.strptime(date_part, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def normalize_text(value: Any) -> str:
    """Safely normalize arbitrary text to a single stripped, single-spaced string."""
    if value is None:
        return ""
    if isinstance(value, (tuple, list)):
        try:
            value = " ".join(str(v) for v in value)
        except Exception:
            value = str(value)
    try:
        s = str(value)
    except Exception:
        s = ""
    return " ".join(s.split()).strip()


def normalize_player_name(name: str) -> str:
    """Strip jersey numbers and flip 'Last, First' -> 'First Last'."""
    s = normalize_text(name)
    s = re.sub(r"\b\d+\b", "", s)
    s = " ".join(s.split())
    if "," in s:
        parts = [p.strip() for p in s.split(",")]
        if len(parts) == 2:
            s = f"{parts[1]} {parts[0]}"
    return s


def normalize_school_key(name: str) -> str:
    """Normalize school names so small differences still match."""
    s = normalize_text(name).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    stop_words = {"university", "college", "of", "the"}
    tokens = [t for t in s.split() if t and t not in stop_words]
    return " ".join(tokens)


def canonical_name(name: str) -> str:
    """Canonicalize names for joining: strip punctuation, lowercase, sort unique tokens."""
    if not name:
        return ""
    s = normalize_text(name).lower()
    s = re.sub(r"[^a-z\s]", " ", s)
    tokens = [t for t in s.split() if t]
    if not tokens:
        return ""
    return " ".join(sorted(set(tokens)))


def normalize_class(raw: str) -> str:
    """Normalize a class string to Fr/R-Fr/So/R-So/Jr/R-Jr/Sr/R-Sr/Gr/Fifth."""
    if not raw:
        return ""
    s = normalize_text(raw).lower()
    s = re.sub(r"[^a-z0-9\s\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    if s in ("fy", "first year", "first-year", "firstyear"):
        return "Fr"
    if s in ("rfr", "r-fy", "r fy", "rf", "rfy", "r-fr"):
        return "R-Fr"

    redshirt = "redshirt" in s or s.startswith("r ")
    base = ""
    if "fresh" in s or re.search(r"\bfr\b", s) or "first year" in s or re.search(r"\bfy\b", s):
        base = "Fr"
    elif "soph" in s or re.search(r"\bso\b", s):
        base = "So"
    elif "junior" in s or re.search(r"\bjr\b", s):
        base = "Jr"
    elif "senior" in s or re.search(r"\bsr\b", s):
        base = "Sr"
    elif "fifth" in s or "5th" in s or "6th" in s or "sixth" in s:
        base = "Fifth"
    elif "grad" in s or re.search(r"\bgr\b", s):
        base = "Gr"

    if base in {"Gr", "Fifth"}:
        return base
    if not base:
        return ""
    if redshirt and base in {"Fr", "So", "Jr", "Sr"}:
        return f"R-{base}"
    return base


def normalize_height(raw: str) -> str:
    """Normalize height into 'F-I' (e.g. '6-2'); empty string if unparseable."""
    s = normalize_text(raw)
    if not s:
        return ""
    s = s.replace("’", "'").replace("`", "'")
    s = s.replace('"', "").replace("in", "")
    s = s.strip().lower()
    m = re.match(r"(\d+)\s*'\s*(\d+)", s) or re.match(r"(\d+)\s*[-]\s*(\d+)", s)
    if m:
        feet, inches = int(m.group(1)), int(m.group(2))
        if 0 <= inches < 12 and 4 <= feet <= 7:
            return f"{feet}-{inches}"
    nums = re.findall(r"\d+", s)
    if len(nums) == 2:
        feet, inches = int(nums[0]), int(nums[1])
        if 0 <= inches < 12 and 4 <= feet <= 7:
            return f"{feet}-{inches}"
    return ""


def height_to_inches(raw: Any) -> int | None:
    """Convert a height ('6-2', "6'2\"") to total inches, or None."""
    norm = normalize_height(raw)
    if not norm:
        return None
    feet, inches = norm.split("-")
    return int(feet) * 12 + int(inches)


def extract_position_codes(position: str) -> set[str]:
    """Map a raw roster position string into normalized codes: S, RS, OH, MB, DS."""
    p_raw = normalize_text(position)
    if not p_raw:
        return set()
    p = p_raw.lower().replace(".", " ").strip()
    staff_keywords = [
        "coach", "assistant", "director", "consultant", "coordinator", "analyst",
        "trainer", "manager", "intern", "video", "strength", "operations",
        "development", "technical", "volunteer", "graduate assistant",
    ]
    if any(kw in p for kw in staff_keywords):
        return set()

    parts = re.split(r"[\/,;]+", p)
    tokens: list[str] = []
    for part in parts:
        tokens.extend(part.split())
    joined = " ".join(tokens)
    codes: set[str] = set()

    if "setter" in joined or re.search(r"\bs\b", joined):
        codes.add("S")
    if (
        "opp" in joined or "opposite" in joined or "right side" in joined
        or "rightside" in joined or re.search(r"\brs\b", joined) or re.search(r"\brh\b", joined)
    ):
        codes.add("RS")
    if "middle" in joined or re.search(r"\bmb\b", joined) or re.search(r"\bmh\b", joined):
        codes.add("MB")
    if (
        "outside" in joined or "pin" in joined or "left side" in joined or "left" in joined
        or re.search(r"\boh\b", joined) or re.search(r"\bls\b", joined)
    ):
        codes.add("OH")
    if (
        "libero" in joined or "defensive specialist" in joined or re.search(r"\bds\b", joined)
        or any(t in {"l", "lib"} for t in tokens)
    ):
        codes.add("DS")
    if "utility" in joined or re.search(r"\butl\b", joined) or re.search(r"\buu\b", joined):
        codes.update({"OH", "DS"})
    if ("opposite" in joined or "opp" in joined) and "setter" in joined:
        codes.update({"S", "RS"})
    if ("opposite" in joined or "opp" in joined) and "middle" in joined:
        codes.update({"RS", "MB"})
    return codes
