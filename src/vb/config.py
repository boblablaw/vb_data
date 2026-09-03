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

    # --- Accounts / auth (JWT bearer, mirrors travel-rewards conventions) ---
    jwt_secret: str = "dev-secret-change-me"
    jwt_expiry_days: int = 7
    # Bootstrap admin created/promoted on startup if no admin exists yet. NOTE: the login endpoint
    # validates emails, which rejects reserved TLDs like `.local` — so the default uses a real TLD.
    # Override ADMIN_EMAIL in .env with your actual address in production.
    admin_email: str = "admin@vballr.app"
    admin_password: str = "VBallr-change-me"
    # Public base URL used to build email-verification links.
    base_url: str = "http://localhost:8091"

    # --- Email (Resend SMTP; blank host => log-only dev fallback) ---
    mail_host: str = ""
    mail_port: int = 587
    mail_username: str = ""
    mail_password: str = ""
    mail_from: str = "noreply@vballr.local"

    # --- WebAuthn / passkeys ---
    webauthn_rp_id: str = "localhost"
    webauthn_rp_name: str = "VBallr"
    webauthn_origin: str = "http://localhost:8091"

    # --- Observability (Sentry; blank DSN => disabled, so local dev / tests are untouched) ---
    sentry_dsn: str = ""
    sentry_environment: str = "development"       # set "production" on the box
    sentry_traces_sample_rate: float = 0.25       # fraction of requests traced (protects free-tier quota)
    sentry_profiles_sample_rate: float = 0.0      # CPU profiling; opt-in later
    sentry_release: str = ""                      # deploy sets vb-data@<git-sha>; blank => vb-data@<version>

    # --- Web analytics (privacy-first, anonymous; blank src => disabled, so local dev / tests get
    # no tracking). Provider-agnostic: the tag is injected server-side so the site id / token stays
    # out of this public repo. Set both on the box:
    #   Umami:      SRC=https://cloud.umami.is/script.js            ATTRS=data-website-id="<id>"
    #   Plausible:  SRC=https://plausible.io/js/script.js           ATTRS=data-domain="vballr.com"
    #   Cloudflare: SRC=https://static.cloudflareinsights.com/beacon.min.js  ATTRS=data-cf-beacon='{"token":"<t>"}'
    analytics_script_src: str = ""
    analytics_script_attrs: str = ""

    # NOTE: the MCP access token and the (single, admin-only) Anthropic API key are NOT env
    # settings — they are set at runtime via the admin panel and stored in the app_settings table.

    # Paths (relative to repo root unless absolute)
    vb_teams_json: str = "data/teams.json"
    # Scrape -> load staging: raw scraped CSVs live here and double as resume ledgers.
    vb_staging_dir: str = "staging"

    @property
    def teams_json_path(self) -> Path:
        return self._abs(self.vb_teams_json)

    @property
    def staging_dir(self) -> Path:
        return self._abs(self.vb_staging_dir)

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
