"""Broadcast/TV-network ingest: normalization registry, feed parsers, and the DB matcher.

The pure functions (``networks.normalize``, the ICS/playlist/EPG parsers) are unit-tested against
small fixture strings — no network. The Postgres-backed matcher (``ingest_broadcasts``) is
self-seeding on a far-future sentinel season, fed a canned ``FeedBroadcast`` list, and asserts the
rows land on the right (date + team pair) and surface on ``/games``.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from vb.db import engine, session_scope
from vb.load import ingest_broadcasts
from vb.models import Broadcast, Conference, Contest, Schedule, Team
from vb.scrape import broadcasts as B
from vb.scrape import networks as N


# --- networks.normalize (pure) -----------------------------------------------------------------
def test_normalize_folds_local_affiliate_to_parent_net():
    assert N.normalize("AL | Birmingham | FOX 6 WBRC") == ("FOX", "fox")


def test_normalize_drops_disney_plus():
    assert N.normalize("Disney+ Events 48") is None
    assert N.normalize("Disney Plus") is None


def test_normalize_specific_before_generic():
    assert N.normalize("ESPN+") == ("ESPN+", "espn-plus")
    assert N.normalize("ESPNU") == ("ESPNU", "espnu")
    assert N.normalize("ESPN") == ("ESPN", "espn")
    # The ICS "Streaming Video" host can't tell linear ESPN from ESPN+, so it gets an honest label.
    assert N.normalize("www.espn.com") == ("ESPN/ESPN+", "espn")


def test_normalize_streaming_platforms_and_text_only():
    assert N.normalize("bigtenplus.com") == ("B1G+", "b1g-plus")
    assert N.normalize("watch.themw.com") == ("MW+", "mw-plus")
    # Known but logo-less network -> text pill (logo_key None).
    assert N.normalize("CBS Sports Network") == ("CBS Sports Network", None)


def test_normalize_unknown_returns_none():
    assert N.normalize("Totally Made Up Channel") is None
    assert N.normalize("") is None
    assert N.normalize(None) is None


def test_normalize_bare_webstream_host_falls_back_to_web_stream():
    # Unrecognized school/conference webstream hosts (ICS "Streaming Video" link reduced to its host)
    # surface as a generic "Web stream" rather than being dropped.
    assert N.normalize("uconnhuskies.com") == ("Web stream", None)
    assert N.normalize("psacsportsdigitalnetwork.com") == ("Web stream", None)
    assert N.normalize("csura.ms") == ("Web stream", None)
    # A known platform host still resolves via its rule first (fallback never reached).
    assert N.normalize("watch.themw.com") == ("MW+", "mw-plus")
    # Junk URL-wrapper hosts and non-host strings still drop.
    assert N.normalize("urldefense.com") is None
    assert N.normalize("Totally Made Up Channel") is None


# --- feed parsers (pure) -----------------------------------------------------------------------
def test_parse_ics_extracts_pair_host_and_day():
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VEVENT\r\n"
        "DTSTART:20260907T230000Z\r\n"
        "SUMMARY:Women's Volleyball Nebraska at  Wisconsin\r\n"
        "DESCRIPTION:Streaming Video: https://bigtenplus.com/game/1https://theacc.com/cal\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    [fb] = B._parse_ics(ics)
    assert fb.team_a == "Nebraska" and fb.team_b == "Wisconsin"
    assert fb.network_raw == "bigtenplus.com" and fb.source == "ics"
    assert fb.date == "2026-09-07"  # 23:00Z stays same UTC day in ET (7pm)


def test_parse_ics_skips_events_without_streaming_video():
    ics = (
        "BEGIN:VEVENT\r\n"
        "DTSTART:20260907T230000Z\r\n"
        "SUMMARY:Women's Volleyball Foo vs Bar\r\n"
        "END:VEVENT\r\n"
    )
    assert B._parse_ics(ics) == []


def test_parse_playlist_event_channel_without_keyword():
    # Dedicated event channels carry the matchup in the name and no "volleyball" word.
    m3u = (
        '#EXTINF:-1 tvg-name="BIG10+ 02: Nebraska vs Wisconsin" group-title="USA | BIG10+",BIG10+ 02\n'
        "http://x\n"
        '#EXTINF:-1 tvg-name="NFL Football: Lions vs Bears" group-title="USA | FOX",FOX\n'
        "http://x\n"
    )
    fbs = B.parse_playlist(m3u)
    assert len(fbs) == 1  # football row dropped by the other-sport guard
    fb = fbs[0]
    assert (fb.team_a, fb.team_b) == ("Nebraska", "Wisconsin")
    assert fb.network_raw == "BIG10+" and fb.source == "playlist"
    assert fb.channel_no == "02"  # event-feed slot captured from "BIG10+ 02: ..."


def test_parse_playlist_uses_paren_subnet_and_slot():
    # A combined "SEC+ / ACC extra" multiplex names the real sub-net in parens; prefer it, and keep
    # the slot number. Otherwise an ACCNX game would be mislabeled "SEC Network+".
    m3u = (
        '#EXTINF:-1 tvg-name="SEC+ / ACC extra 07: Duke vs North Carolina (ACCNX) @ Sep 07 6:00PM ET"'
        ' group-title="USA | SEC+ / ACC EXTRA",SEC+ / ACC extra 07\n'
        "http://x\n"
    )
    [fb] = B.parse_playlist(m3u)
    assert (fb.team_a, fb.team_b) == ("Duke", "North Carolina")
    assert fb.network_raw == "ACCNX"  # parenthetical sub-net wins over the combined group label
    assert fb.channel_no == "07"


def test_parse_epg_live_marker_and_channel():
    epg = (
        "<tv>"
        '<channel id="c1"><display-name>Big Ten Network FHD</display-name></channel>'
        '<programme channel="c1" start="20260907190000 -0400">'
        "<title>Volleyball: Nebraska vs Wisconsin ᴸᶦᵛᵉ</title></programme>"
        '<programme channel="c1" start="20260908010000 -0400">'
        "<title>Volleyball: Nebraska vs Wisconsin</title></programme>"
        "</tv>"
    )
    live, replay = B.parse_epg(epg)
    assert live.is_live is True and replay.is_live is False
    assert live.network_raw == "Big Ten Network FHD"
    assert live.date == "2026-09-07"  # 19:00 -0400


# --- ingest_broadcasts (Postgres-backed, self-seeding) -----------------------------------------
def _db_available() -> bool:
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")

SEASON = 2105  # far-future sentinel, distinct from other test files
TODAY = __import__("datetime").date(2105, 9, 5)


def _wipe(s):
    s.execute(text("DELETE FROM broadcasts WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM schedule WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM contests WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM teams WHERE name LIKE '_BX_TEAM%'"))
    s.execute(text("DELETE FROM conferences WHERE name LIKE '_BX_CONF%'"))


@pytest.fixture
def seed():
    with session_scope() as s:
        _wipe(s)
    with session_scope() as s:
        conf = Conference(name="_BX_CONF"); s.add(conf); s.flush()
        # Nonsense short_names that still resolve via slug/normalize, won't collide with real teams.
        a = Team(name="_BX_TEAM_A", short_name="Zqbxa Tech", conference_id=conf.id)
        b = Team(name="_BX_TEAM_B", short_name="Zqbxb St.", conference_id=conf.id)
        c = Team(name="_BX_TEAM_C", short_name="Zqbxc", conference_id=conf.id)
        s.add_all([a, b, c]); s.flush()
        # Played contest A vs B on 09-04; upcoming A vs B on 09-08 (both perspectives).
        s.add(Contest(contest_id="7300001", season=SEASON, date="2105-09-04 19:00",
                      home_team_id=a.id, away_team_id=b.id, home_sets_won=3, away_sets_won=0))
        s.add_all([
            Schedule(season=SEASON, team_id=a.id, opponent_team_id=b.id, opponent_name="_BX_TEAM_B",
                     date="2105-09-08", game_time="07:00 PM", site="home"),
            Schedule(season=SEASON, team_id=b.id, opponent_team_id=a.id, opponent_name="_BX_TEAM_A",
                     date="2105-09-08", game_time="07:00 PM", site="away"),
        ])
        out = {"a": a.id, "b": b.id, "c": c.id}
    yield out
    with session_scope() as s:
        _wipe(s)


def _fb(team_a, team_b, date, network_raw, source="ics", is_live=True, channel_no=None):
    return B.FeedBroadcast(team_a=team_a, team_b=team_b, date=date, network_raw=network_raw,
                           source=source, is_live=is_live, channel_no=channel_no)


@requires_db
def test_ingest_matches_contest_and_schedule(seed):
    feeds = [
        _fb("Zqbxa Tech", "Zqbxb St.", "2105-09-04", "bigtenplus.com"),      # -> played contest
        _fb("Zqbxb St.", "Zqbxa Tech", "2105-09-08", "www.espn.com"),         # -> upcoming (reversed)
        _fb("Nowhere St.", "Elsewhere", "2105-09-04", "www.espn.com"),        # unresolved pair
        _fb("Zqbxa Tech", "Zqbxb St.", "2105-09-04", "Disney+ Events 3"),     # dropped by normalize
    ]
    with session_scope() as s:
        res = ingest_broadcasts(s, SEASON, days_back=3, days_ahead=10, today=TODAY, feeds=feeds)
    assert res["unresolved"] >= 1
    with session_scope() as s:
        rows = s.query(Broadcast).filter(Broadcast.season == SEASON).all()
        by_date = {(r.game_date, r.network) for r in rows}
        assert ("2105-09-04", "B1G+") in by_date
        assert ("2105-09-08", "ESPN/ESPN+") in by_date
        assert all(r.network != "Disney+" for r in rows)  # never stored
        assert all(r.team_a_id < r.team_b_id for r in rows)  # ordered pair


@requires_db
def test_ingest_is_idempotent_and_refreshes(seed):
    feeds1 = [_fb("Zqbxa Tech", "Zqbxb St.", "2105-09-08", "bigtenplus.com")]
    with session_scope() as s:
        ingest_broadcasts(s, SEASON, today=TODAY, feeds=feeds1)
    with session_scope() as s:
        first = s.query(Broadcast).filter(Broadcast.season == SEASON).count()
    # Re-run with a DIFFERENT network for the same game: the window is cleared, so the old tag goes.
    feeds2 = [_fb("Zqbxa Tech", "Zqbxb St.", "2105-09-08", "www.espn.com")]
    with session_scope() as s:
        ingest_broadcasts(s, SEASON, today=TODAY, feeds=feeds2)
    with session_scope() as s:
        nets = {r.network for r in s.query(Broadcast).filter(Broadcast.season == SEASON).all()}
    assert first == 1
    assert nets == {"ESPN/ESPN+"}  # B1G+ replaced, not accumulated


@requires_db
def test_ics_wins_over_tps_for_same_network(seed):
    # Same game, same normalized network from ICS (live) then EPG (replay): one row, ICS/live wins.
    feeds = [
        _fb("Zqbxa Tech", "Zqbxb St.", "2105-09-08", "bigtenplus.com", source="ics", is_live=True),
        _fb("Zqbxa Tech", "Zqbxb St.", "2105-09-08", "Big Ten Network Plus", source="epg",
            is_live=False),
    ]
    with session_scope() as s:
        ingest_broadcasts(s, SEASON, today=TODAY, feeds=feeds)
    with session_scope() as s:
        rows = s.query(Broadcast).filter(Broadcast.season == SEASON,
                                         Broadcast.network == "B1G+").all()
    assert len(rows) == 1
    assert rows[0].source == "ics" and rows[0].is_live is True


@requires_db
def test_tps_specific_espn_flavor_refines_ics_generic(seed):
    # ICS gives the opaque espn.com generic; TPS EPG names the real flavor. Card shows just ESPN+.
    feeds = [
        _fb("Zqbxa Tech", "Zqbxb St.", "2105-09-08", "www.espn.com", source="ics"),
        _fb("Zqbxa Tech", "Zqbxb St.", "2105-09-08", "ESPN+", source="epg"),
    ]
    with session_scope() as s:
        ingest_broadcasts(s, SEASON, today=TODAY, feeds=feeds)
    with session_scope() as s:
        nets = {r.network for r in s.query(Broadcast).filter(Broadcast.season == SEASON)}
    assert nets == {"ESPN+"}  # generic "ESPN/ESPN+" dropped in favor of the specific flavor


@requires_db
def test_espn_plus_carried_extra_refines_ics_generic(seed):
    # ICS gives the opaque espn.com generic; TPS names ACC Network Extra, which is delivered *via*
    # ESPN+ (it is literally the "+" arm of the generic). Card shows just ACCNX, not both.
    feeds = [
        _fb("Zqbxa Tech", "Zqbxb St.", "2105-09-08", "www.espn.com", source="ics"),
        _fb("Zqbxa Tech", "Zqbxb St.", "2105-09-08", "ACCNX", source="playlist"),
    ]
    with session_scope() as s:
        ingest_broadcasts(s, SEASON, today=TODAY, feeds=feeds)
    with session_scope() as s:
        nets = {r.network for r in s.query(Broadcast).filter(Broadcast.season == SEASON)}
    assert nets == {"ACC Network Extra"}  # generic dropped in favor of the ESPN+-carried extra


@requires_db
def test_generic_espn_kept_when_no_specific_flavor(seed):
    feeds = [_fb("Zqbxa Tech", "Zqbxb St.", "2105-09-08", "www.espn.com", source="ics")]
    with session_scope() as s:
        ingest_broadcasts(s, SEASON, today=TODAY, feeds=feeds)
    with session_scope() as s:
        nets = {r.network for r in s.query(Broadcast).filter(Broadcast.season == SEASON)}
    assert nets == {"ESPN/ESPN+"}  # honest fallback survives when nothing more specific exists


@requires_db
def test_live_dateless_tps_slot_pins_to_todays_meeting(seed):
    # A TPS "on now" slot carries no date. When the pair plays twice in-window it normally can't be
    # placed. But a *live* slot is airing today, so it pins to the meeting near today — and its
    # specific ESPN flavor then refines the ICS generic. (Mirrors Kansas–Wichita St., who play twice.)
    when = __import__("datetime").date(2105, 9, 8)
    with session_scope() as s:  # add a second, later meeting of the same pair (both now in-window)
        s.add_all([
            Schedule(season=SEASON, team_id=seed["a"], opponent_team_id=seed["b"],
                     opponent_name="_BX_TEAM_B", date="2105-09-15", game_time="07:00 PM", site="home"),
            Schedule(season=SEASON, team_id=seed["b"], opponent_team_id=seed["a"],
                     opponent_name="_BX_TEAM_A", date="2105-09-15", game_time="07:00 PM", site="away"),
        ])
    feeds = [
        _fb("Zqbxa Tech", "Zqbxb St.", "2105-09-08", "www.espn.com", source="ics"),      # dated generic
        _fb("Zqbxa Tech", "Zqbxb St.", None, "ESPN+", source="playlist", is_live=True),  # dateless, on now
    ]
    with session_scope() as s:
        ingest_broadcasts(s, SEASON, days_back=3, days_ahead=10, today=when, feeds=feeds)
    with session_scope() as s:
        rows = s.query(Broadcast).filter(Broadcast.season == SEASON).all()
        by = {(r.game_date, r.network) for r in rows}
    assert ("2105-09-08", "ESPN+") in by                       # live slot pinned to today + refined
    assert not any(r.game_date == "2105-09-15" for r in rows)  # the far meeting stays untagged


@requires_db
def test_dateless_slot_not_pinned_when_not_live(seed):
    # A non-live (replay) dateless slot must NOT be pinned — with two meetings it can't be placed.
    when = __import__("datetime").date(2105, 9, 8)
    with session_scope() as s:
        s.add(Schedule(season=SEASON, team_id=seed["a"], opponent_team_id=seed["b"],
                       opponent_name="_BX_TEAM_B", date="2105-09-15", game_time="07:00 PM", site="home"))
    feeds = [_fb("Zqbxa Tech", "Zqbxb St.", None, "ESPN+", source="playlist", is_live=False)]
    with session_scope() as s:
        res = ingest_broadcasts(s, SEASON, days_back=3, days_ahead=10, today=when, feeds=feeds)
        assert res["unresolved"] >= 1
        assert s.query(Broadcast).filter(Broadcast.season == SEASON).count() == 0


@requires_db
def test_games_payload_carries_broadcast_tags(client, seed):
    feeds = [_fb("Zqbxa Tech", "Zqbxb St.", "2105-09-08", "www.espn.com")]
    with session_scope() as s:
        ingest_broadcasts(s, SEASON, today=TODAY, feeds=feeds)
    game = client.get("/games", params={"season": SEASON, "date": "2105-09-08"}).json()[0]
    assert game["broadcasts"] == [{"network": "ESPN/ESPN+", "logo_key": "espn", "channel_no": None}]


@requires_db
def test_channel_no_carried_to_payload(client, seed):
    feeds = [_fb("Zqbxa Tech", "Zqbxb St.", "2105-09-08", "ESPN+", source="playlist", channel_no="45")]
    with session_scope() as s:
        ingest_broadcasts(s, SEASON, today=TODAY, feeds=feeds)
    game = client.get("/games", params={"season": SEASON, "date": "2105-09-08"}).json()[0]
    assert game["broadcasts"] == [{"network": "ESPN+", "logo_key": "espn-plus", "channel_no": "45"}]


@requires_db
def test_past_game_tag_frozen_when_feed_drops_it(seed):
    # The contest is on 2105-09-04 (yesterday, since TODAY=2105-09-05). First run tags it.
    with session_scope() as s:
        ingest_broadcasts(s, SEASON, today=TODAY, feeds=[_fb("Zqbxa Tech", "Zqbxb St.",
                          "2105-09-04", "bigtenplus.com")])
    with session_scope() as s:
        assert s.query(Broadcast).filter(Broadcast.season == SEASON,
                                         Broadcast.game_date == "2105-09-04").count() == 1
    # Next day the feeds no longer carry the past game. Its tag must survive (frozen), NOT be deleted.
    with session_scope() as s:
        ingest_broadcasts(s, SEASON, today=TODAY, feeds=[])
    with session_scope() as s:
        assert s.query(Broadcast).filter(Broadcast.season == SEASON,
                                         Broadcast.game_date == "2105-09-04").count() == 1


@requires_db
def test_future_game_tag_removed_when_feed_drops_it(seed):
    # Future games (>= today) still refresh: a tag that disappears from the feed is dropped.
    with session_scope() as s:
        ingest_broadcasts(s, SEASON, today=TODAY, feeds=[_fb("Zqbxa Tech", "Zqbxb St.",
                          "2105-09-08", "bigtenplus.com")])
    with session_scope() as s:
        ingest_broadcasts(s, SEASON, today=TODAY, feeds=[])
    with session_scope() as s:
        assert s.query(Broadcast).filter(Broadcast.season == SEASON,
                                         Broadcast.game_date == "2105-09-08").count() == 0
