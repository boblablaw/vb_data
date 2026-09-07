"""Player-photo scraping: pure roster/og:image parsers + an end-to-end loader test.

The parsers run offline against small HTML fixtures. The loader test seeds a team + players and
drives ``scrape_player_photos`` through a fake httpx client (roster HTML + tiny image bytes), so it
exercises the real fetch -> parse -> match -> download -> photo_path chain without touching the
network. DB tests skip when Postgres is unreachable.
"""
from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text

from vb.db import engine, session_scope
from vb.load import photos as photos_mod
from vb.load import scrape_player_photos
from vb.models import Conference, Player, Team
from vb.scrape.photos import og_image_from_html, parse_roster

BASE = "https://school.test/sports/womens-volleyball/roster"

# A SIDEARM-style roster: two players. Jane's real headshot is lazy in data-src (src is a
# placeholder .svg we must ignore); Mary's is the largest srcset candidate. The trailing anchor is a
# site-logo link whose href isn't /roster/<slug>/<id>, so it must be skipped entirely.
SIDEARM_HTML = """
<html><body>
  <div class="sidearm-roster-player">
    <a href="/roster/jane-smith/101"><img src="/img/placeholder.svg"
        data-src="https://cdn.example.com/jane.jpg"></a>
    <div class="sidearm-roster-player-jersey-number">#7</div>
    <a href="/roster/jane-smith/101">Jane Smith</a>
  </div>
  <div class="sidearm-roster-player">
    <a href="/roster/mary-jones/102"><img src="/img/placeholder.svg"
        srcset="https://cdn.example.com/mary_small.jpg 320w, https://cdn.example.com/mary_large.jpg 640w"></a>
    <span class="number">#12</span>
    <a href="/roster/mary-jones/102">Mary Jones</a>
  </div>
  <a href="/sports/womens-volleyball"><img src="/logos/site.svg"></a>
</body></html>
"""

# A non-SIDEARM player detail page: no roster-card image, headshot only in og:image.
OG_HTML = """
<html><head>
  <meta property="og:image" content="https://cdn.example.com/og-headshot.jpg">
</head><body>Zoe Ng</body></html>
"""


def test_parse_roster_prefers_lazy_and_skips_chrome():
    hits = parse_roster(SIDEARM_HTML, BASE)
    assert [h.name for h in hits] == ["Jane Smith", "Mary Jones"]  # site-logo anchor excluded
    jane, mary = hits
    assert jane.image_url == "https://cdn.example.com/jane.jpg"        # data-src, not placeholder src
    assert jane.jersey == 7
    assert jane.player_url.endswith("/roster/jane-smith/101")
    assert mary.image_url == "https://cdn.example.com/mary_large.jpg"  # largest srcset candidate
    assert mary.jersey == 12


def test_parse_roster_skips_svg_logo_images():
    # A card whose only image is an SVG logo -> no usable roster image (would need og fallback).
    html = """<div><a href="/roster/al-pha/1"><img data-src="/logos/team.svg"></a>
              <a href="/roster/al-pha/1">Al Pha</a></div>"""
    (hit,) = parse_roster(html, BASE)
    assert hit.name == "Al Pha"
    assert hit.image_url is None


# WMT/Nuxt convention (Clemson, Nebraska, ...): player link is /sports/<sport>/roster/player/<slug>
# (no numeric id) and the headshot is an imgproxy URL. This is what the rendered DOM looks like.
WMT_HTML = """
<html><body>
  <li class="player">
    <a href="/sports/volleyball/roster/player/addi-rains"><img
        src="https://clemsontigers.com/imgproxy/abc123/roster/addi.jpg"></a>
    <div class="jersey">#5</div>
    <a href="/sports/volleyball/roster/player/addi-rains">Addi Rains</a>
  </li>
</body></html>
"""


def test_parse_roster_wmt_player_slug_convention():
    (hit,) = parse_roster(WMT_HTML, "https://clemsontigers.com/sports/volleyball/roster")
    assert hit.name == "Addi Rains"
    assert hit.jersey == 5
    assert hit.image_url == "https://clemsontigers.com/imgproxy/abc123/roster/addi.jpg"
    assert hit.player_url.endswith("/roster/player/addi-rains")


def test_og_image_from_html():
    assert og_image_from_html(OG_HTML, BASE) == "https://cdn.example.com/og-headshot.jpg"
    assert og_image_from_html("<html><head></head></html>", BASE) is None


# --------------------------------------------------------------------------- loader (needs Postgres)

def _db_available() -> bool:
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")

SEASON = 1902  # sentinel, distinct from other DB tests


class _FakeResp:
    def __init__(self, url, *, text_body="", content=b"", ct="text/html"):
        self.url = url
        self.text = text_body
        self.content = content
        self.headers = {"content-type": ct}

    def raise_for_status(self):
        pass


class _FakeClient:
    """Minimal stand-in for httpx.Client: serves canned responses keyed by URL."""

    def __init__(self, routes, **_kw):
        self.routes = routes

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def get(self, url):
        try:
            return self.routes[url]
        except KeyError:
            raise httpx.HTTPError(f"no route for {url}") from None


@pytest.fixture
def seeded():
    with session_scope() as s:
        _wipe(s)
    ids = {}
    with session_scope() as s:
        conf = Conference(name="_TEST_PHOTO_CONF")
        s.add(conf)
        s.flush()
        team = Team(name="_TEST_PHOTO_TEAM", conference_id=conf.id, website=BASE)
        s.add(team)
        s.flush()
        for name, num, ncaa in [
            ("Jane Smith", 7, "P101"),
            ("Mary Jones", 12, "P102"),
            ("Zoe Absent", 99, "P199"),   # not on the roster page -> stays without a photo
        ]:
            p = Player(team_id=team.id, season=SEASON, name=name, number=num, ncaa_player_id=ncaa)
            s.add(p)
            s.flush()
            ids[ncaa] = p.id
    yield ids
    with session_scope() as s:
        _wipe(s)


def _wipe(s):
    s.execute(text("DELETE FROM players WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM teams WHERE name = '_TEST_PHOTO_TEAM'"))
    s.execute(text("DELETE FROM conferences WHERE name = '_TEST_PHOTO_CONF'"))


@requires_db
def test_scrape_player_photos_matches_and_downloads(seeded, tmp_path, monkeypatch):
    ids = seeded
    routes = {
        BASE: _FakeResp(BASE, text_body=SIDEARM_HTML),
        "https://cdn.example.com/jane.jpg": _FakeResp(
            "https://cdn.example.com/jane.jpg", content=b"\xff\xd8jpegbytes", ct="image/jpeg"),
        "https://cdn.example.com/mary_large.jpg": _FakeResp(
            "https://cdn.example.com/mary_large.jpg", content=b"\x89PNGbytes", ct="image/png"),
    }
    monkeypatch.setattr(photos_mod.settings, "vb_photos_dir", str(tmp_path / "player_photos"))
    monkeypatch.setattr(photos_mod.httpx, "Client", lambda **kw: _FakeClient(routes, **kw))

    with session_scope() as s:
        res = scrape_player_photos(s, SEASON, only_teams=["_TEST_PHOTO_TEAM"])

    assert res == {"teams": 1, "matched": 2, "downloaded": 2, "players": 3}

    # Files landed under the (patched) static dir, keyed by stable ncaa_player_id, ext by content-type.
    assert (tmp_path / "player_photos" / "P101.jpg").read_bytes() == b"\xff\xd8jpegbytes"
    assert (tmp_path / "player_photos" / "P102.png").read_bytes() == b"\x89PNGbytes"

    with session_scope() as s:
        jane = s.get(Player, ids["P101"])
        mary = s.get(Player, ids["P102"])
        zoe = s.get(Player, ids["P199"])
        assert jane.photo_path == "assets/player_photos/P101.jpg"
        assert mary.photo_path == "assets/player_photos/P102.png"
        assert zoe.photo_path is None
