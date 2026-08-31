# Hosting vb_data on an OCI (Ampere ARM) instance

Runs the pipeline unattended: a **daily 01:00** incremental scrape (adds only new contests) plus a
**weekly** roster refresh, via systemd timers. Postgres runs in Docker on the same box.

> **The crux for ARM:** stats.ncaa.org sits behind Akamai. The bypass normally uses *real Google
> Chrome*, which **has no ARM/Linux build**. On this instance we use **system Chromium, headful,
> under Xvfb**. That is not guaranteed to pass Akamai — **run the probe in step 5 before wiring the
> timers.** Fallbacks are in the last section.

All commands run over `ssh oracle`. The default Oracle Linux user is `opc`; adjust paths/user if
yours differ (the systemd units and scripts assume `/home/opc/vb_data` and user `opc`).

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

### One-time setup

**a. Dedicated deploy keypair** (on your laptop) — don't reuse a personal key:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/vb_oci_deploy -N "" -C "vb_data-gha-deploy"
# authorize the public half on the box:
ssh oracle 'cat >> ~/.ssh/authorized_keys' < ~/.ssh/vb_oci_deploy.pub
```

**b. Repo secrets** (`gh` from the repo dir; `OCI_HOST` is the box's public IP/hostname):
```bash
gh secret set OCI_SSH_KEY < ~/.ssh/vb_oci_deploy      # the PRIVATE key
gh secret set OCI_HOST     --body "<public-ip-or-host>"
gh secret set OCI_USER     --body "opc"
# optional overrides (defaults 22 and /home/opc/vb_data):
# gh secret set OCI_SSH_PORT --body "22"
# gh secret set OCI_REPO_PATH --body "/home/opc/vb_data"
```
Until `OCI_HOST` is set the workflow still runs but **skips** the deploy step (green, not
failed).

**c. Open SSH to GitHub's runners.** GitHub-hosted runners have no fixed IPs, so port 22 must
be reachable from the internet:
- **OCI security list / NSG:** add an ingress rule for TCP 22 from `0.0.0.0/0` (or a tighter
  range if you use a self-hosted runner / VPN).
- **Host firewall:** `sudo firewall-cmd --permanent --add-service=ssh && sudo firewall-cmd --reload`.

> **Security note.** Exposing 22 broadly + storing a deploy private key in a *public* repo's
> secrets is the pragmatic tradeoff for this action. Harden as you like: restrict the key in
> `~/.ssh/authorized_keys` with `command="cd /home/opc/vb_data && ./scripts/deploy.sh",no-port-forwarding,no-pty <key>`
> so it can *only* deploy; and/or move to a self-hosted runner or Tailscale and keep 22 closed.

### Verify
Push a trivial commit (or **Actions → Deploy to OCI → Run workflow**), watch the run, then:
```bash
ssh oracle 'cd ~/vb_data && git rev-parse --short HEAD'   # should match the pushed commit
```

## Fallbacks if the probe is BLOCKED
1. **x86 Chromium under emulation on the ARM box:** `sudo dnf install -y qemu-user-static`
   (registers binfmt), then run an x86_64 Chromium via `VB_CHROME_EXECUTABLE`. Slow but keeps
   everything on OCI.
2. **Split scrape from hosting:** keep Postgres + timers logic on OCI, but run the *scrape* on an
   x86 machine or your Mac (real/ARM Chrome works there) and load into the OCI DB over an SSH
   tunnel (`ssh -L 5435:localhost:5435 oracle`, point `DATABASE_URL` at the tunnel). Most reliable.
3. Revisit stealth: try a spoofed `userAgentData` brand set and a residential-looking UA before
   giving up on native ARM Chromium.
