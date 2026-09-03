#!/usr/bin/env bash
#
# Sentry cron check-ins for the scrape timers, so a missed or failed nightly/hourly/weekly run
# raises an alert (the pipeline blind spot: the API is monitored, but nothing watched the scrapers).
#
# No-op when SENTRY_DSN is unset — local/off-box runs stay clean. Every network call is best-effort
# (short timeout, errors swallowed): a Sentry hiccup must NEVER fail a scrape. The monitor is
# auto-created/updated on first check-in via the embedded monitor_config, so there is no UI step.
#
# The DSN is parsed at runtime from the environment (it lives in .env, out of the public repo) —
# nothing about the org/project/key is hardcoded here.
#
# Usage (from a job script):
#   source "$SCRIPT_DIR/lib/sentry_cron.sh"
#   CHECKIN_ID="$(sentry_checkin_start "vb-daily-scrape" "0 1 * * *" 180 30)"
#   trap 'sentry_checkin_finish "vb-daily-scrape" "$CHECKIN_ID" "$([ $? -eq 0 ] && echo ok || echo error)"' EXIT
#   ... do work ...

_SENTRY_CRON_URLBASE=""
_SENTRY_CRON_KEY=""

# Parse SENTRY_DSN (https://<key>@<host>/<project_id>) into the cron check-in base URL + key.
# Returns non-zero (and leaves the globals empty) when the DSN is unset or unparseable.
_sentry_cron_init() {
  _SENTRY_CRON_URLBASE=""
  _SENTRY_CRON_KEY=""
  [ -n "${SENTRY_DSN:-}" ] || return 1
  local rest hostpath host proj
  rest="${SENTRY_DSN#https://}"
  _SENTRY_CRON_KEY="${rest%%@*}"
  hostpath="${rest#*@}"
  host="${hostpath%%/*}"
  proj="${hostpath##*/}"
  proj="${proj%%\?*}"   # drop any ?querystring
  if [ -z "$_SENTRY_CRON_KEY" ] || [ -z "$host" ] || [ -z "$proj" ]; then
    _SENTRY_CRON_KEY=""
    return 1
  fi
  _SENTRY_CRON_URLBASE="https://${host}/api/${proj}/cron"
}

# sentry_checkin_start <slug> <crontab_value> [max_runtime_min] [checkin_margin_min]
# Sends an in-progress check-in (upserting the monitor) and echoes the check-in id (empty on
# failure/disabled). Always exits 0 so callers can use it in `VAR="$(...)"` under `set -e`.
sentry_checkin_start() {
  _sentry_cron_init || { echo ""; return 0; }
  local slug="$1" cron="$2" maxrt="${3:-180}" margin="${4:-15}"
  local url resp id
  url="${_SENTRY_CRON_URLBASE}/${slug}/${_SENTRY_CRON_KEY}/"
  resp="$(curl -sS --max-time 10 -X POST "$url" \
    -H 'Content-Type: application/json' \
    --data "{\"status\":\"in_progress\",\"environment\":\"${SENTRY_ENVIRONMENT:-production}\",\"monitor_config\":{\"schedule\":{\"type\":\"crontab\",\"value\":\"${cron}\"},\"timezone\":\"America/New_York\",\"checkin_margin\":${margin},\"max_runtime\":${maxrt},\"failure_issue_threshold\":1,\"recovery_threshold\":1}}" 2>/dev/null || true)"
  id="$(printf '%s' "$resp" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([0-9a-fA-F-]*\)".*/\1/p')"
  echo "$id"
}

# sentry_checkin_finish <slug> <checkin_id> <status>   (status = ok | error)
# Closes the check-in started above. No-op when disabled or when the start check-in had no id.
sentry_checkin_finish() {
  _sentry_cron_init || return 0
  local slug="$1" id="$2" status="$3"
  [ -n "$id" ] || return 0
  local url="${_SENTRY_CRON_URLBASE}/${slug}/${_SENTRY_CRON_KEY}/${id}/"
  curl -sS --max-time 10 -X PUT "$url" \
    -H 'Content-Type: application/json' \
    --data "{\"status\":\"${status}\"}" >/dev/null 2>&1 || true
}
