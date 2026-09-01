"""Central configuration (env-driven via pydantic-settings)."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root = two levels up from this file (src/vb/config.py -> repo root).
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    # Postgres. psycopg (v3) driver.
    database_url: str = "postgresql+psycopg://vb:vb@localhost:5435/vb"

    # Scrape pacing
    vb_min_delay: float = 3.0
    vb_max_delay: float = 6.0

    # Fetch resilience. A single flaky page load should not abort a 347-team sweep:
    # retry each page a few times (with growing backoff), then let the scrape skip it.
    vb_fetch_retries: int = 3                # attempts per page before giving up
    vb_fetch_retry_backoff: float = 2.0      # base seconds between attempts (grows per attempt)
    vb_scrape_fail_threshold: float = 0.25   # fraction of teams that may fail before a run is failed

    # Browser (Akamai bypass). Defaults suit a laptop with real Google Chrome; on hosts
    # without Chrome (e.g. ARM servers, which have no Google Chrome build) point these at
    # system Chromium and run headful under Xvfb.
    vb_headless: bool = True
    vb_chrome_channel: str | None = "chrome"     # "chromium"/"" to use non-Chrome builds
    vb_chrome_executable: str | None = None       # e.g. /usr/bin/chromium-browser

    # Paths (relative to repo root unless absolute)
    vb_teams_json: str = "data/teams.json"
    vb_exports_dir: str = "exports"

    @property
    def teams_json_path(self) -> Path:
        return self._abs(self.vb_teams_json)

    @property
    def exports_dir(self) -> Path:
        return self._abs(self.vb_exports_dir)

    @staticmethod
    def _abs(p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else REPO_ROOT / path


settings = Settings()


# Default Fantasy Points weights (weighted sum of per-game/season counting stats). The API's
# fantasy leaderboard uses these unless a caller overrides individual weights via `w_<stat>` query
# params. Error stats carry negative weight. Keys MUST be counting-stat columns present on BOTH
# player_game_stats and the player_season_stats matview (see vb.api.routers.stats.FANTASY_STATS).
FANTASY_WEIGHTS: dict[str, float] = {
    "kills": 1.0,
    "aces": 1.5,
    "digs": 0.5,
    "assists": 0.25,
    "block_solos": 1.0,
    "block_assists": 0.5,
    "errors": -0.5,
    "serr": -0.5,
    "rerr": -0.25,
    "berr": -0.25,
    "bhe": -0.25,
}
