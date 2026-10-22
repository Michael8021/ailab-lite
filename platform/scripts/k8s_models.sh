#!/usr/bin/env bash
# Publish platform/models.toml, or $MODELS_FILE (the model catalog users pick from) as the platform-models ConfigMap,
# then restart the services that read it. The catalog holds no keys, only the names of AILAB_KEY_* variables.
#   bash scripts/k8s_models.sh            apply + restart api and agent
#   NO_RESTART=1 bash scripts/k8s_models.sh   apply only (before the first deployment)
set -euo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
NS=platform
# MODELS_FILE: another catalog (deploy.sh passes platform/models.local.toml when there is one)
FILE=${MODELS_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/models.toml}

# refuse a broken catalog before it reaches the cluster
python3 - "$FILE" <<'PY'
import sys, tomllib
data = tomllib.load(open(sys.argv[1], "rb"))
ids = [m["id"] for m in data["models"]]
assert ids and len(ids) == len(set(ids)), "model ids must be non-empty and unique"
print("catalog ok:", ids)
PY

$KUBECTL get ns "$NS" >/dev/null 2>&1 || $KUBECTL create ns "$NS"
$KUBECTL -n "$NS" create configmap platform-models --from-file=models.toml="$FILE" --dry-run=client -o yaml | $KUBECTL apply -f -
if [ -z "${NO_RESTART:-}" ]; then
  for d in api agent; do $KUBECTL -n "$NS" rollout restart "deploy/platform-$d" >/dev/null 2>&1 || true; done
  for d in api agent; do $KUBECTL -n "$NS" rollout status "deploy/platform-$d" --timeout=240s 2>&1 | tail -1 || true; done
fi
