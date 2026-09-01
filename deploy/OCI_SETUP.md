# Hosting vb_data on an OCI (Ampere ARM) instance

Runs the pipeline unattended: a **daily 01:00** incremental scrape (adds only new contests) plus a
**weekly** roster refresh, via systemd timers. Postgres runs in Docker on the same box.

> **The crux for ARM:** stats.ncaa.org sits behind Akamai. The bypass normally uses *real Google
> Chrome*, which **has no ARM/Linux build**. On this instance we use **system Chromium, headful,
> under Xvfb**. That is not guaranteed to pass Akamai — **run the probe in step 5 before wiring the
> timers.** Fallbacks are in the last section.

All commands run over `ssh oracle`.

> **Live deployment (this repo's box).** `ssh oracle` → **`ubuntu@mediaserver`, Ubuntu 24.04,
> aarch64**, Docker already installed. Provisioned with `apt` (not `dnf`), venv on Python 3.12,
> and **snap Chromium** (`/usr/bin/snap install chromium` → `/snap/bin/chromium`) headful under
> Xvfb — **the Akamai probe passed at 45 KB**, so native ARM scraping works here (no fallback
> needed). The systemd units below are set to `User=ubuntu` / `/home/ubuntu/vb_data`. The `dnf`
> lines in §1 are for a stock Oracle-Linux box; the apt equivalents are called out inline.

## 1. System packages

Oracle Linux (dnf):
```bash
sudo dnf install -y git chromium xorg-x11-server-Xvfb \
    liberation-fonts dejavu-sans-fonts python3.11 python3.11-pip
# Docker Engine + compose plugin (Oracle Linux):
sudo dnf install -y dnf-utils
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker opc      # log out/in so `docker` works without sudo
```
(Ubuntu instead? swap to `apt install chromium-browser xvfb fonts-liberation ...` and Docker's apt repo; set `VB_CHROME_EXECUTABLE=/usr/bin/chromium-browser`.)

Confirm the Chromium path: `which chromium || which chromium-browser`.

## 2. Clone + Python env
```bash
cd ~ && git clone <your-remote> vb_data && cd vb_data
python3.11 -m venv venv
./venv/bin/pip install -e ".[dev]"
./venv/bin/playwright install chromium     # ARM build; used only if VB_CHROME_EXECUTABLE unset
```

## 3. `.env` on the server
```bash
cat > ~/vb_data/.env <<'EOF'
DATABASE_URL=postgresql+psycopg://vb:vb@localhost:5435/vb
VB_HEADLESS=false
VB_CHROME_CHANNEL=
VB_CHROME_EXECUTABLE=/usr/bin/chromium-browser
# VB_SEASON=2026        # pin the fall year; otherwise derived from the date
EOF
```
`VB_HEADLESS=false` + Xvfb (the scripts wrap scrapes in `xvfb-run`) is the anti-Akamai posture.

## 4. Database up + schema
```bash
cd ~/vb_data
docker compose up -d db
./venv/bin/alembic upgrade head
```

## 5. PROBE — does Chromium beat Akamai here? (gate)
```bash
cd ~/vb_data
xvfb-run -a ./venv/bin/python - <<'PY'
from vb.fetch import fetch_html
html = fetch_html("https://stats.ncaa.org/teams/624993", wait_selectors=("table",))
print("BLOCKED" if ("Access Denied" in html and len(html) < 2000) else f"OK ({len(html)} bytes)")
PY
```
- **OK** → continue to step 6.
- **BLOCKED** → do **not** wire the timers yet; jump to *Fallbacks* below.

> **Akamai rate-limits aggressive scraping.** A burst of many fetches from one IP can earn a
> temporary hard block (a ~300-byte "Access Denied" for every request) that persists for a while
> even with real Chrome. The daily incremental run only fetches a handful of new pages, so it's
> low-risk — but a cold *full* season scrape hits 360+ team pages. That's exactly why step 6 seeds
> from your laptop. If you do trigger a block, wait it out (tens of minutes to hours) and consider
> raising the pacing: `VB_MIN_DELAY` / `VB_MAX_DELAY` in `.env` (defaults 3.0 / 6.0 seconds).

## 6. Seed from your laptop (so day-1 is incremental, not a multi-hour full scrape)
From the laptop (which already has a populated DB + resume CSVs):
```bash
# DB dump -> restore into the instance's container
pg_dump "postgresql://vb:vb@localhost:5435/vb" -Fc -f /tmp/vb.dump
scp /tmp/vb.dump oracle:/tmp/vb.dump
ssh oracle 'docker exec -i vb_data_postgres pg_restore -U vb -d vb --clean --if-exists < /tmp/vb.dump'

# Resume ledgers + team dimension
scp exports/ncaa_wvb_*_d1_*.csv oracle:~/vb_data/exports/
scp data/teams.json             oracle:~/vb_data/data/teams.json
```
(If you skip this, the first daily run just scrapes the whole season once, then is incremental.)

## 7. Timezone + install the timers
```bash
sudo timedatectl set-timezone America/New_York     # so 01:00 means *your* 1am
sudo cp ~/vb_data/deploy/vb-daily.service ~/vb_data/deploy/vb-daily.timer \
        ~/vb_data/deploy/vb-weekly-rosters.service ~/vb_data/deploy/vb-weekly-rosters.timer \
        /etc/systemd/system/
# Edit the two .service files if your user/path is not opc:/home/opc/vb_data
sudo systemctl daemon-reload
sudo systemctl enable --now vb-daily.timer vb-weekly-rosters.timer
```

## 8. Verify
```bash
sudo systemctl start vb-daily.service          # run once by hand
journalctl -u vb-daily -f                       # watch it
systemctl list-timers vb-daily.timer            # next run should read 01:00
# Idempotency: run again immediately — it should add 0 new contests.
sudo systemctl start vb-daily.service && journalctl -u vb-daily -n 20 --no-pager
```
Row check:
```bash
docker exec vb_data_postgres psql -U vb -d vb -c \
  "select count(distinct contest_id) from player_game_stats;"
```

## What runs, and why it's "add only"
`scripts/daily_update.sh`: `scrape game-stats` (skips every contest already in the CSV **and** the
DB — see `src/vb/scrape/game_stats.py`), then `load-game-stats` (upserts), `derive-cumulative`,
`enrich rpi`, and CSV exports. Re-running never re-fetches a known box score. The scrape still
loads each team page once to *discover* new contests, so a run takes ~20–35 min + new games.

`scripts/weekly_rosters.sh`: resets the roster CSV and re-scrapes so new mid-season players get
rostered (otherwise their game stats are skipped at load). Loader upserts — no duplicates.

## 9. Continuous deployment (auto-update on push to main)

`.github/workflows/deploy.yml` SSHes into this box on every push to `main` and runs
`scripts/deploy.sh` (git reset to `origin/main`, reinstall deps only if `pyproject.toml`
changed, `alembic upgrade head`). It runs as the app user with **no sudo** and does **not**
touch the systemd units — the timer jobs pick up new code on their next firing, so there's
nothing to restart. If a push changes `deploy/*.service|*.timer`, deploy.sh prints a warning
and you re-sync them manually (step 7); this avoids clobbering the host-specific `User=`/path
edits.

This setup keeps **port 22 closed to the internet** (the runner reaches the box over
Tailscale) and **pins the deploy key to a forced command** (it can *only* run deploy.sh, never
get a shell). Defense in depth: network layer + auth layer.

**a. Dedicated deploy keypair, pinned to a forced command** (on your laptop) — don't reuse a
personal key:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/vb_oci_deploy -N "" -C "vb_data-gha-deploy"

# authorize it on the box with a FORCED command: whatever the runner sends is ignored;
# only deploy.sh runs, with no shell, no port-forwarding. (path = /home/ubuntu/vb_data here)
ssh oracle 'read PUB; umask 077; mkdir -p ~/.ssh; grep -qF vb_data-gha-deploy ~/.ssh/authorized_keys ||
  printf "command=\"cd /home/ubuntu/vb_data && ./scripts/deploy.sh\",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding %s\n" "$PUB" >> ~/.ssh/authorized_keys' < ~/.ssh/vb_oci_deploy.pub
```

**b. Tailscale on the box** — already installed on `mediaserver`. For a fresh box:
```bash
ssh oracle 'curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up'
ssh oracle 'tailscale ip -4'    # note the 100.x.y.z addr / MagicDNS name (here: mediaserver / 100.84.11.31)
```
No OCI security-list or firewall change is needed — Tailscale is **outbound-only** (UDP 41641,
or a DERP relay over 443). Leave 22 closed to `0.0.0.0/0`.

> **Gotcha — Tailscale SSH intercepts port 22.** `mediaserver` runs Tailscale SSH
> (`RunSSH: true`), so tailscaled *intercepts* port 22 on the tailnet and authenticates by
> ACL/identity — the runner's OpenSSH forced-command key never applies, and an untrusted
> `tag:ci` node gets `ssh: handshake failed: EOF`. Fix: expose OpenSSH on a **second port that
> Tailscale SSH does not touch** (here `2222`) and deploy over that. On Ubuntu 24.04 `sshd` is
> **socket-activated**, so a `Port` line in `sshd_config` is ignored — add the port to
> `ssh.socket` instead (restarting the socket only affects *new* connections, so no lockout):
> ```bash
> ssh oracle 'printf "[Socket]\nListenStream=0.0.0.0:2222\nListenStream=[::]:2222\n" |
>   sudo tee /etc/systemd/system/ssh.socket.d/10-vb-deploy.conf >/dev/null &&
>   sudo systemctl daemon-reload && sudo systemctl restart ssh.socket'
> ssh oracle 'ss -tlnp | grep :2222'   # expect 0.0.0.0:2222 AND [::]:2222 (IPv4 needed — tailnet is IPv4)
> ```
> Then set `OCI_SSH_PORT=2222` (step d). 2222 stays off the public internet — reachable only over
> the tailnet, same as 22. (Alternative: use Tailscale SSH itself via an ACL `ssh` accept rule for
> `tag:ci`, dropping the OpenSSH key — but that gives up the forced-command hardening.)

**c. Tailscale auth key for CI** (simplest; the redesigned OAuth "Trust credentials" wizard hides
the tag picker). First declare the tag in the policy file
([admin → Access controls](https://login.tailscale.com/admin/acls)):
```jsonc
"tagOwners": {"tag:Oracle": [], "tag:ci": []},
```
This tailnet uses the wildcard `grants` block (`src:["*"] dst:["*"]`), so `tag:ci` can already
reach `mediaserver:22` — no extra grant needed. Then **Settings → Keys → Generate auth key**:
check **Reusable** + **Ephemeral**, select tag **`tag:ci`**, and copy the `tskey-auth-…` value.
(Auth keys expire in ≤90 days — regenerate and re-set the secret when it lapses. OAuth clients
don't expire but the current UI makes tagging them awkward.)

**d. Repo secrets** (`gh` from the repo dir):
```bash
gh secret set OCI_SSH_KEY   < ~/.ssh/vb_oci_deploy         # the PRIVATE key
gh secret set OCI_USER      --body "ubuntu"
gh secret set OCI_REPO_PATH --body "/home/ubuntu/vb_data"
gh secret set TS_AUTHKEY    --body "tskey-auth-…"
gh secret set OCI_SSH_PORT  --body "2222"                  # OpenSSH's non-Tailscale-SSH port (step b)
# set OCI_HOST LAST — it's the on/off gate; the workflow skips until it exists:
gh secret set OCI_HOST      --body "mediaserver"
```
(Omit `OCI_SSH_PORT` only on a box *without* Tailscale SSH, where 22 reaches OpenSSH directly.)
Until `OCI_HOST` is set the workflow still runs but **skips** the deploy step (green, not
failed). `OCI_HOST` is the box's *Tailscale* name/IP — not its public address.

> **Why this shape.** GitHub-hosted runners have no fixed IPs, so the usual alternative is
> opening 22 to `0.0.0.0/0`. Tailscale avoids that entirely, and the forced command means even
> a leaked deploy key can't do anything but redeploy the current `main`. If you ever want to
> drop the SSH key too, Tailscale SSH can authenticate via ACLs instead — but the forced-command
> key is the tighter default here.

### Verify
Push a trivial commit (or **Actions → Deploy to OCI → Run workflow**), watch the run, then:
```bash
ssh oracle 'cd ~/vb_data && git rev-parse --short HEAD'   # should match the pushed commit
```
Test the forced command locally too — it should run deploy.sh and refuse a shell:
```bash
ssh -i ~/.ssh/vb_oci_deploy -o IdentitiesOnly=yes opc@<box-tailscale-name> whoami
# prints deploy.sh output, NOT "opc" — the requested `whoami` is ignored.
```

## 10. Public web UI (fantasy site over HTTPS)

The read API + vanilla-JS fantasy UI (`src/vb/api/`, served at `/ui/`) run as a **container**
(`vb-api`) behind the box's shared **edge-caddy** — the same `caddy:2-alpine` that already fronts
`tr-api` and the Docmost wiki on `0.0.0.0:80/443`. No second Caddy, no firewall change (80/443 are
already public), no systemd unit. The scrapers keep running from the host venv via the timers; this
container only serves FastAPI (its image ships **no** Playwright browsers).

`scripts/deploy.sh` builds/starts the container automatically **only on this box** — it keys off the
external `deploy_web` docker network (created by the travel-rewards stack). Off the box that network
is absent and the step is a clean no-op. It runs:
```bash
docker compose -f docker-compose.yml -f docker-compose.remote.yml up -d --build vb-api
```
`vb-api` joins two networks: the compose `default` (to reach `db:5432`) and external `deploy_web`
(so edge-caddy resolves it at `vb-api:8091`). It publishes **no** host port. See
`docker-compose.remote.yml`.

### 10a. Read-only DB role (one-time, on the box)
The public container connects as a least-privilege, read-only role with a short statement timeout —
never the `vb` owner the loader/timers use. Run once against the DB container:
```bash
docker exec -i vb_data_postgres psql -U vb -d vb <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'vb_ro') THEN
    CREATE ROLE vb_ro LOGIN PASSWORD 'vb_ro';   -- override + set VB_API_DATABASE_URL for a stronger secret
  END IF;
END $$;
GRANT CONNECT ON DATABASE vb TO vb_ro;
GRANT USAGE ON SCHEMA public TO vb_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO vb_ro;                 -- tables, matview, contest_weeks view
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO vb_ro;
ALTER ROLE vb_ro SET statement_timeout = '5s';                        -- cap any pathological query
SQL
```
PG is firewalled from the internet (only reachable inside the docker network / on-box `5435`), so the
default `vb_ro:vb_ro` credential is low-value — but to use a stronger password, set it above and put
`VB_API_DATABASE_URL=postgresql+psycopg://vb_ro:<pass>@db:5432/vb` in an env file the compose reads.
The container defaults to `vb_ro` regardless (`DATABASE_URL` in `docker-compose.remote.yml`).

### 10b. edge-caddy site block (one-time)
Pick a hostname on the existing duckdns account (e.g. `vbfantasy.duckdns.org`) and point its A record
at the box's reserved public IP. Then append a site block to edge-caddy's Caddyfile
(`/home/ubuntu/travel-rewards-api/deploy/Caddyfile`, same file the wiki uses) and reload:
```caddyfile
vbfantasy.duckdns.org {
    reverse_proxy vb-api:8091
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        -Server
    }
    log { output stdout; format json; level INFO }
}
```
```bash
# reload without dropping the other sites (run from the travel-rewards deploy dir):
docker exec edge-caddy caddy reload --config /etc/caddy/Caddyfile
```
Caddy fetches a Let's Encrypt cert for the new host automatically. `vb-api` must already be up and on
`deploy_web` (it is, after a deploy) so Caddy can resolve the upstream by name.

### 10c. Verify
```bash
curl -sS https://vbfantasy.duckdns.org/health          # {"status":"ok"}
curl -sSI https://vbfantasy.duckdns.org/ | grep -i location   # 307 -> /ui/
# then open https://vbfantasy.duckdns.org/ui/ in a browser
```
SSH stays tailnet-only; only 80/443 are public. A redeploy (`push to main`) rebuilds the container
cleanly; the Caddyfile block and `vb_ro` role are one-time and survive redeploys.

## Fallbacks if the probe is BLOCKED
1. **x86 Chromium under emulation on the ARM box:** `sudo dnf install -y qemu-user-static`
   (registers binfmt), then run an x86_64 Chromium via `VB_CHROME_EXECUTABLE`. Slow but keeps
   everything on OCI.
2. **Split scrape from hosting:** keep Postgres + timers logic on OCI, but run the *scrape* on an
   x86 machine or your Mac (real/ARM Chrome works there) and load into the OCI DB over an SSH
   tunnel (`ssh -L 5435:localhost:5435 oracle`, point `DATABASE_URL` at the tunnel). Most reliable.
3. Revisit stealth: try a spoofed `userAgentData` brand set and a residential-looking UA before
   giving up on native ARM Chromium.
