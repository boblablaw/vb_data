#!/usr/bin/env bash
#
# Deploy the current main onto this host. Run by the GitHub Actions deploy workflow over SSH
# (or by hand). Idempotent: fast when nothing changed. Runs as the app user — no sudo.
#
# Intentionally does NOT touch systemd units (they carry host-specific User=/path edits); if
# deploy/ changes it warns you to re-sync them manually. The daily/weekly jobs are timer-driven
# oneshots, so there is no long-running service to restart — the next firing uses the new code.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
cd "$REPO"

echo "=== deploy @ $(date -Is) ==="
before_deps="$(git rev-parse HEAD:pyproject.toml 2>/dev/null || echo none)"
before_units="$(git rev-parse HEAD:deploy 2>/dev/null || echo none)"

git fetch --prune origin
git reset --hard origin/main
echo "now at $(git rev-parse --short HEAD)"

# shellcheck disable=SC1091
source venv/bin/activate

after_deps="$(git rev-parse HEAD:pyproject.toml 2>/dev/null || echo none)"
if [ "$before_deps" != "$after_deps" ]; then
  echo "pyproject changed -> reinstalling deps"
  pip install -e ".[dev]" --quiet
fi

# Migrations are idempotent; ensure the DB is up first.
docker compose up -d db >/dev/null 2>&1 || true
alembic upgrade head

# Public web UI (only on the OCI box, which has the shared edge-caddy `deploy_web` network).
# Off the box the network is absent, so this is a clean no-op. See deploy/OCI_SETUP.md §10.
if docker network inspect deploy_web >/dev/null 2>&1; then
  # Tag this deploy as its own Sentry release so errors carry a per-deploy version (regression /
  # "first seen in" tracking). Written into .env (the container reads it via env_file) rather than
  # printed; idempotent — any prior SENTRY_RELEASE line is replaced. Truncate-in-place to keep the
  # file's existing 600 perms/ownership.
  if [ -f .env ]; then
    rel="vb-data@$(git rev-parse --short HEAD)"
    tmp="$(mktemp)"
    grep -v '^SENTRY_RELEASE=' .env > "$tmp" || true
    printf 'SENTRY_RELEASE=%s\n' "$rel" >> "$tmp"
    cat "$tmp" > .env
    rm -f "$tmp"
    echo "tagged Sentry release: $rel"
  fi
  echo "deploy_web present -> (re)building vb-api container"
  docker compose -f docker-compose.yml -f docker-compose.remote.yml up -d --build vb-api
else
  echo "deploy_web network absent -> skipping vb-api container (not the public host)"
fi

after_units="$(git rev-parse HEAD:deploy 2>/dev/null || echo none)"
if [ "$before_units" != "$after_units" ]; then
  echo "WARNING: deploy/ unit files changed. Re-copy them to /etc/systemd/system/ and run"
  echo "         'sudo systemctl daemon-reload' (skipped here to preserve host-specific edits)."
fi

echo "=== deploy complete: $(git rev-parse --short HEAD) @ $(date -Is) ==="
