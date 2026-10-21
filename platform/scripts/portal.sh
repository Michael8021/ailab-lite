#!/usr/bin/env bash
# Portal dev server (exec keeps the PID on next, so dev_down can stop it).
set -euo pipefail
cd "$(dirname "$0")/../portal"
# AILAB_API_BASE (server-side, not NEXT_PUBLIC_*) is where the Portal proxy finds the Platform API.
exec ./node_modules/.bin/next dev --hostname 127.0.0.1 --port 3000
