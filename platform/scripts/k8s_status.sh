#!/usr/bin/env bash
# Status of the k3s deployment (after `kubectl apply -f deploy/k8s/platform.yaml`).
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

$KUBECTL -n platform get pods,pvc,svc 2>/dev/null || echo "platform namespace not found"
echo
for target in "30080/api" "8081/broker" "8090/opensandbox"; do
  port=${target%%/*}
  name=${target##*/}
  printf '%-12s ' "$name"
  curl -fsS "http://127.0.0.1:$port/healthz" 2>/dev/null || \
    curl -fsS "http://127.0.0.1:$port/health" 2>/dev/null || echo "DOWN"
  echo
done
