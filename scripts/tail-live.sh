#!/usr/bin/env bash
#
# Tail live per-request activity from the production vb-api container.
#
# The app logs one structured line per request (`vb.api: METHOD /path -> STATUS (NNNms)`); this
# follows that stream in real time so you can watch users interact with the site.
#
# Two gotchas this wraps up:
#   - grep block-buffers when its stdout is a pipe (as it is over ssh), so lines don't appear until
#     ~4KB pile up. `--line-buffered` flushes each line immediately.
#   - `docker logs -f` alone replays the entire history first; `--tail 0` shows only new activity.
#
# Usage:
#   scripts/tail-live.sh            # live requests, health/asset/ui noise filtered out
#   scripts/tail-live.sh --all      # everything (include /health, /assets/, /ui/)
#   scripts/tail-live.sh --history  # replay recent history too, then follow (default: new-only)
#
# Env:
#   VB_SSH_HOST   ssh host/alias for the OCI box (default: oracle)
#   VB_CONTAINER  container name (default: vb-api)
set -euo pipefail

HOST="${VB_SSH_HOST:-oracle}"
CONTAINER="${VB_CONTAINER:-vb-api}"

show_all=0
tail_opt="--tail 0"
for arg in "$@"; do
  case "$arg" in
    --all)     show_all=1 ;;
    --history) tail_opt="" ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown arg: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# Build the remote pipeline. Always keep only our structured lines; optionally drop the noisy paths.
remote="docker logs -f ${tail_opt} ${CONTAINER} 2>&1 | grep --line-buffered 'vb.api:'"
if [ "$show_all" -eq 0 ]; then
  remote="${remote} | grep --line-buffered -vE 'health|/assets/|/ui/'"
fi

echo "→ tailing ${CONTAINER} on ${HOST} (Ctrl-C to stop)$([ "$show_all" -eq 0 ] && echo ' — health/asset/ui noise hidden')" >&2
exec ssh "$HOST" "$remote"
