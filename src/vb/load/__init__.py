"""CSV -> Postgres loaders (idempotent upserts). Scrape writes CSVs; loaders ingest them."""
from .coaches import load_coaches
from .enrichment import enrich_avca, enrich_logos, enrich_photos, enrich_rpi, snapshot_rankings
from .game_stats import load_game_stats
from .ncaa_com_games import map_ncaa_games
from .pbp import load_pbp
from .rosters import load_rosters
from .schedule import load_schedule
from .season_stats import load_season_stats
from .teams import load_teams

__all__ = [
    "enrich_avca",
    "enrich_logos",
    "enrich_photos",
    "enrich_rpi",
    "load_coaches",
    "load_game_stats",
    "load_pbp",
    "load_rosters",
    "load_schedule",
    "load_season_stats",
    "load_teams",
    "map_ncaa_games",
    "snapshot_rankings",
]
