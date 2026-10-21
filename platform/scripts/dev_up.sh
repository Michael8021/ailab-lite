#!/usr/bin/env bash
# Start the whole platform on the dev VM (host processes, one sandbox at a time).
set -euo pipefail

ROOT=${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
PLATFORM=${PLATFORM:-$ROOT/platform}
cd "$PLATFORM"

if [ ! -x .venv/bin/python ]; then
  echo "[dev_up] creating venv"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -e .
else
  .venv/bin/pip install -q -e . --no-deps
fi

mkdir -p var/log var/run var/storage
[ -f .env ] || cp .env.example .env

redis-cli ping >/dev/null 2>&1 || { echo "[dev_up] redis is not running on 127.0.0.1:6379"; exit 1; }

# Host mode and k3s mode both bind 8081/8080; refuse to start on top of a live deployment.
if command -v k3s >/dev/null 2>&1; then
  if KUBECONFIG=${KUBECONFIG:-$HOME/.kube/config} k3s kubectl -n platform get deploy platform-broker >/dev/null 2>&1; then
    echo "[dev_up] k3s platform deployment is running; use scripts/k8s_down.sh before host mode"
    exit 1
  fi
fi

if grep -q '^AILAB_SANDBOX_PROVIDER=opensandbox' .env 2>/dev/null; then
  OSB_DOMAIN=$(grep '^AILAB_OSB_DOMAIN=' .env | cut -d= -f2- || true)
  OSB_DOMAIN=${OSB_DOMAIN:-127.0.0.1:8090}
  curl -fsS "http://$OSB_DOMAIN/health" >/dev/null 2>&1 || {
    echo "[dev_up] opensandbox chosen but server at $OSB_DOMAIN is down; run: bash scripts/osb_up.sh"
    exit 1
  }
fi

if ! docker image inspect "${AILAB_SANDBOX_IMAGE:-harbor.ailab.local/sandbox/sbx-office:lite}" >/dev/null 2>&1; then
  echo "[dev_up] WARN sandbox image missing -> falling back to AILAB_SANDBOX_PROVIDER=mock"
  export AILAB_SANDBOX_PROVIDER=mock
fi

bash scripts/dev_down.sh >/dev/null 2>&1 || true

start() {
  local name=$1
  shift
  nohup "$@" >"var/log/$name.log" 2>&1 &
  echo $! >"var/run/$name.pid"
  echo "[dev_up] $name pid=$(cat "var/run/$name.pid")"
}

start api .venv/bin/uvicorn ailab_platform.api.main:app --host 127.0.0.1 --port 8080 --workers 1
start broker .venv/bin/uvicorn ailab_platform.broker.main:app --host 127.0.0.1 --port 8081 --workers 1
start agent .venv/bin/uvicorn ailab_platform.agent.main:app --host 127.0.0.1 --port 8082 --workers 1
start worker .venv/bin/uvicorn ailab_platform.worker.main:app --host 127.0.0.1 --port 8083 --workers 1

for port in 8080 8081 8082 8083; do
  ok=0
  for _ in $(seq 1 40); do
    if curl -fsS "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; then ok=1; break; fi
    sleep 0.5
  done
  [ "$ok" = 1 ] || { echo "[dev_up] service on :$port failed to start"; tail -30 "var/log/"*.log; exit 1; }
done

if [ -d portal/node_modules ]; then
  # Server-side only: the Portal proxies the API; the browser never sees this URL or any token.
  export AILAB_API_BASE=${AILAB_API_BASE:-http://127.0.0.1:8080}
  start portal bash scripts/portal.sh
else
  echo "[dev_up] portal/node_modules missing; run: cd portal && npm install"
fi

echo
echo "Platform API : http://127.0.0.1:8080/docs"
echo "Broker       : http://127.0.0.1:8081/healthz"
echo "Agent        : http://127.0.0.1:8082/healthz"
echo "Worker       : http://127.0.0.1:8083/healthz"
echo "Portal       : http://127.0.0.1:3000"
echo "Login token  : ${AILAB_DEV_TOKEN:-dev-demo-token}   (paste it on the Portal login page)"
