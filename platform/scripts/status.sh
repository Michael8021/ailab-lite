#!/usr/bin/env bash
set -uo pipefail
for port in 8080 8081 8082 8083; do
  printf '%-6s ' "$port"
  curl -fsS "http://127.0.0.1:$port/healthz" 2>/dev/null || echo "DOWN"
  echo
done
printf '%-6s ' "3000"
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3000 2>/dev/null || echo "DOWN"
printf '%-6s ' "osb"
curl -fsS http://127.0.0.1:8090/health 2>/dev/null || echo "DOWN"
echo
