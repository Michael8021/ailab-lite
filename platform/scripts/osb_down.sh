#!/usr/bin/env bash
# Stop the OpenSandbox dev server (leaves platform services untouched).
set -uo pipefail

pids=$(pgrep -f "bin/opensandbox-server --config" || true)
if [ -z "$pids" ]; then
  echo "[osb_down] not running"
  exit 0
fi
echo "[osb_down] stopping $pids"
kill $pids 2>/dev/null || true
sleep 1
pids=$(pgrep -f "bin/opensandbox-server --config" || true)
[ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
