#!/usr/bin/env bash
# Stop platform services started by dev_up.sh.
set -uo pipefail

PLATFORM=${PLATFORM:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
cd "$PLATFORM" || exit 0

pids=()
for pidfile in var/run/*.pid; do
  [ -e "$pidfile" ] || continue
  pid=$(cat "$pidfile")
  name=$(basename "$pidfile" .pid)
  pids+=("$pid")
  if kill -0 "$pid" 2>/dev/null; then
    echo "[dev_down] stopping $name ($pid)"
    kill "$pid" 2>/dev/null || true
  fi
done

sleep 1
for pid in "${pids[@]:-}"; do
  kill -9 "$pid" 2>/dev/null || true
done
rm -f var/run/*.pid

# Orphan sandboxes are cleaned by the Broker on the next start; do it here too.
docker ps -aq --filter label=ailab.lease 2>/dev/null | xargs -r docker rm -f >/dev/null 2>&1 || true
