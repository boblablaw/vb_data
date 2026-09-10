"""CSV -> Postgres loaders (idempotent upserts). Scrape writes CSVs; loaders ingest them."""
from .broadcasts import ingest_broadcasts
from .coaches import load_coaches
from .enrichment import (
    enrich_avca,
    enrich_conference_logos,
    enrich_logos,
    enrich_rpi,
    load_avca_archive,
    snapshot_rankings,
)
from .game_stats import load_game_stats
from .ncaa_api_lineups import load_ncaa_lineups
from .ncaa_com_games import map_ncaa_games
from .pbp import load_pbp
from .photos import scrape_player_photos
from .rosters import load_rosters
from .schedule import load_schedule
from .season_stats import load_season_stats
from .teams import load_season_conferences, load_teams

__all__ = [
    "enrich_avca",
    "enrich_conference_logos",
    "enrich_logos",
    "enrich_rpi",
    "ingest_broadcasts",
    "load_avca_archive",
    "load_coaches",
    "load_game_stats",
    "load_ncaa_lineups",
    "load_pbp",
    "load_rosters",
    "load_schedule",
    "load_season_conferences",
    "load_season_stats",
    "load_teams",
    "map_ncaa_games",
    "scrape_player_photos",
    "snapshot_rankings",
]
