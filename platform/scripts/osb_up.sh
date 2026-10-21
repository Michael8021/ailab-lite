#!/usr/bin/env bash
# Start the OpenSandbox dev server used by AILAB_SANDBOX_PROVIDER=opensandbox.
set -euo pipefail

OSB_HOME=${OSB_HOME:-$HOME/osb}
OSB_VENV=${OSB_VENV:-$HOME/osb-venv}
OSB_CONFIG=${OSB_CONFIG:-$OSB_HOME/sandbox.toml}
OSB_PORT=${OSB_PORT:-8090}
OSB_API_KEY=${OSB_API_KEY:-dev-osb-key}

if [ ! -x "$OSB_VENV/bin/opensandbox-server" ]; then
  echo "[osb_up] installing opensandbox-server into $OSB_VENV"
  python3 -m venv "$OSB_VENV"
  "$OSB_VENV/bin/pip" install -q --upgrade pip
  "$OSB_VENV/bin/pip" install -q 'opensandbox-server==1.1.1'
fi

mkdir -p "$(dirname "$OSB_CONFIG")" "$OSB_HOME"
if [ ! -f "$OSB_CONFIG" ]; then
  "$OSB_VENV/bin/opensandbox-server" init-config "$OSB_CONFIG" --example docker
fi
sed -i "s/^port = 8080/port = $OSB_PORT/" "$OSB_CONFIG"
if ! grep -q '^api_key = ' "$OSB_CONFIG"; then
  sed -i "s|^# api_key = .*|api_key = \"$OSB_API_KEY\"|" "$OSB_CONFIG"
fi

if curl -fsS "http://127.0.0.1:$OSB_PORT/health" >/dev/null 2>&1; then
  echo "[osb_up] already healthy on :$OSB_PORT"
  exit 0
fi

cd "$OSB_HOME"
setsid nohup "$OSB_VENV/bin/opensandbox-server" --config "$OSB_CONFIG" > "$OSB_HOME/server.log" 2>&1 < /dev/null &
for _ in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:$OSB_PORT/health" >/dev/null 2>&1; then
    echo "[osb_up] ready on http://127.0.0.1:$OSB_PORT (api key: $OSB_API_KEY)"
    exit 0
  fi
  sleep 0.5
done
echo "[osb_up] failed to start; last log lines:"
tail -30 "$OSB_HOME/server.log"
exit 1
