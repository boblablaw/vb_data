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
