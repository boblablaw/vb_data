"""CSV -> Postgres loaders (idempotent upserts). Scrape writes CSVs; loaders ingest them."""
from .coaches import load_coaches
from .enrichment import enrich_logos, enrich_photos, enrich_rpi
from .game_stats import load_game_stats
from .rosters import load_rosters
from .season_stats import load_season_stats
from .teams import load_teams

__all__ = [
    "enrich_logos",
    "enrich_photos",
    "enrich_rpi",
    "load_coaches",
    "load_game_stats",
    "load_rosters",
    "load_season_stats",
    "load_teams",
]
