#!/usr/bin/env bash
# Create the platform-secrets Secret with random tokens, unless it already exists.
#   bash scripts/k8s_secrets.sh          create (no-op if present)
#   bash scripts/k8s_secrets.sh env      print `export AILAB_*=...` lines (admin password, API token) for scripts
#   DRY_RUN=1 bash scripts/k8s_secrets.sh   show the manifest without touching the cluster
# Model keys: set AILAB_KEY_* in the environment when creating (see models.toml).
set -euo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
NS=platform

case "${1:-create}" in
  env)
    for key in AILAB_ADMIN_PASSWORD AILAB_DEV_TOKEN AILAB_SERVICE_TOKEN; do  # model keys are deliberately not printed
      value=$($KUBECTL -n "$NS" get secret platform-secrets -o "jsonpath={.data.$key}" | base64 -d)
      [ -n "$value" ] || continue  # AILAB_ADMIN_PASSWORD is missing in secrets made before password login existed
      echo "export $key=$value"
    done
    ;;
  create)
    if [ -z "${DRY_RUN:-}" ] && $KUBECTL -n "$NS" get secret platform-secrets >/dev/null 2>&1; then
      echo "[k8s_secrets] platform-secrets already exists; leaving it alone"
      echo "              (rotate: kubectl -n $NS delete secret platform-secrets, rerun, restart the deployments)"
      exit 0
    fi
    # the OpenSandbox dev server (scripts/osb_up.sh) is bound to localhost with this key
    # AILAB_ADMIN_PASSWORD is the first Portal account's password (user name: AILAB_ADMIN_USERNAME in platform-config).
    args=(--from-literal=AILAB_ADMIN_PASSWORD="$(openssl rand -hex 10)"
          --from-literal=AILAB_DEV_TOKEN="$(openssl rand -hex 24)"
          --from-literal=AILAB_SERVICE_TOKEN="$(openssl rand -hex 32)"
          --from-literal=AILAB_OSB_API_KEY="${AILAB_OSB_API_KEY:-dev-osb-key}")
    # Model keys, whichever are set in the environment: AILAB_KEY_DEEPSEEK=sk-... bash scripts/k8s_secrets.sh
    # (the names are the api_key_env values in models.toml). Users never handle these.
    for name in $(compgen -e | grep '^AILAB_KEY_' || true); do args+=(--from-literal="$name=${!name}"); done
    if [ -n "${DRY_RUN:-}" ]; then
      $KUBECTL -n "$NS" create secret generic platform-secrets "${args[@]}" --dry-run=client -o yaml | sed -E 's/(AILAB_[A-Z_]+:).*/\1 <redacted>/'
    else
      $KUBECTL get ns "$NS" >/dev/null 2>&1 || $KUBECTL create ns "$NS"
      $KUBECTL -n "$NS" create secret generic platform-secrets "${args[@]}"
      echo "[k8s_secrets] created. Show the admin password / tokens with:  eval \"\$(bash scripts/k8s_secrets.sh env)\"; echo \$AILAB_ADMIN_PASSWORD"
    fi
    ;;
  *) echo "usage: $0 [create|env]"; exit 2 ;;
esac
