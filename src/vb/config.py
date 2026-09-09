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

    # Scrape pacing. A random pause before every stats.ncaa.org page load; the box raises these
    # well above the defaults (e.g. 8/20) to look human and stay well under any rate flag.
    vb_min_delay: float = 3.0
    vb_max_delay: float = 6.0
    # Hard floor (seconds) between *any* two navigations, enforced regardless of caller/retries so a
    # burst can never form. 0 = disabled (only vb_min/max_delay applies). Box sets ~8.
    vb_request_min_interval: float = 0.0
    # Periodic long "session break": every N page loads, sleep a random vb_break_min..vb_break_max
    # seconds to break up the steady machine cadence Akamai fingerprints. 0 = disabled. Box sets ~40.
    vb_pages_per_break: int = 0
    vb_break_min: float = 30.0
    vb_break_max: float = 90.0

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
    # Abort non-essential subresources (fonts, media, and — except on the headshot-scraping scroll
    # path — images) so far less data crosses the residential proxy: those are ~50-80% of a page's
    # bytes and we only parse the HTML tables. Scripts/XHR are NEVER blocked (Akamai's bot challenge
    # runs in JS). Set VB_BLOCK_RESOURCES=false to load pages whole.
    vb_block_resources: bool = True

    # --- Egress proxy (residential) for stats.ncaa.org ONLY ---
    # The box's public IP is the shared, reserved production IP fronting vballr.com + the wiki +
    # travel-rewards. If Akamai IP-blocks it, all three sites' *serving* IP is the casualty. Routing
    # the real-Chrome fetches through a rotating residential proxy isolates that risk: a block can
    # only ever hit disposable proxy IPs, never the serving IP. Only the Playwright context (which is
    # exclusively stats.ncaa.org traffic) uses this; plain-HTTP scrapers (ncaa.com, AVCA) do not.
    # Blank url => no proxy (local dev unchanged). Set these on the box's .env (git-ignored); never
    # commit them. Standard HTTP proxy, e.g. VB_PROXY_URL=http://gate.provider.com:7000
    vb_proxy_url: str | None = None
    vb_proxy_username: str | None = None
    vb_proxy_password: str | None = None

    # --- Self-hosted ncaa.com wrapper (henrygd/ncaa-api) ---
    # ncaa.com is a DIFFERENT host from the Akamai-blocked stats.ncaa.org, so this sidecar is the
    # resilient primary for schedules / box scores / lineups. The host-venv scrapers reach it on
    # loopback (127.0.0.1:3013 -> container :3000); the vb-api container overrides this to
    # http://ncaa-api:3000 over the compose network. See docker-compose.remote.yml + ~/projects/CLAUDE.md.
    ncaa_api_base_url: str = "http://127.0.0.1:3013"

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

    # --- Broadcast / TV-network ingest (TPS IPTV feeds) ---
    # Personal paid IPTV subscription used only as a FALLBACK behind the public conference ICS
    # calendars. Blank creds => the TPS playlist/EPG feeds are skipped and ICS still runs. Set these
    # on the box's .env (git-ignored); never commit them.
    tps_base_url: str = "https://tps-67.live"
    tps_username: str = ""
    tps_password: str = ""

    @property
    def tps_playlist_url(self) -> str:
        return (f"{self.tps_base_url.rstrip('/')}/get.php?username={self.tps_username}"
                f"&password={self.tps_password}&type=m3u_plus&output=ts")

    @property
    def tps_epg_url(self) -> str:
        return (f"{self.tps_base_url.rstrip('/')}/xmltv.php?username={self.tps_username}"
                f"&password={self.tps_password}")

    @property
    def tps_enabled(self) -> bool:
        return bool(self.tps_username and self.tps_password)

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
    # Player headshots. Kept OUT of the wheel (unlike team logos) — the host scraper writes here and
    # the read-only vb-api container serves it via a bind mount (VB_PHOTOS_DIR=/data/player_photos on
    # the box; default = <repo>/data/player_photos, which is also the compose bind-mount source).
    vb_photos_dir: str = "data/player_photos"

    @property
    def teams_json_path(self) -> Path:
        return self._abs(self.vb_teams_json)

    @property
    def staging_dir(self) -> Path:
        return self._abs(self.vb_staging_dir)

    @property
    def photos_dir(self) -> Path:
        return self._abs(self.vb_photos_dir)

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
