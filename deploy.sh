#!/usr/bin/env bash
# One-command deployment of AILab lite on a machine that already runs k3s + Docker.
#
#   ./deploy.sh                       build everything, deploy to k3s, start the Portal, smoke-test it
#   ./deploy.sh status                what is running and healthy
#   ./deploy.sh down [--yes]          remove the platform from k3s (and stop the Portal)
#
# Options (for `up`):
#   Model (any OpenAI-compatible chat API: DeepSeek, OpenAI, Qwen/DashScope, Kimi, vLLM, Ollama, a gateway ...):
#   --model-base-url URL   the URL before /chat/completions, e.g. https://api.openai.com/v1 or http://10.0.0.5:8000/v1
#   --model-name NAME      the model as that API names it, e.g. gpt-4o-mini, qwen-plus, Qwen/Qwen3-32B
#   --model-key KEY        its API key (optional for keyless local servers). Without --model-base-url the key is for
#                          the catalog's default model (DeepSeek V4.1 Flash out of the box)
#   --model-label TEXT     the name users see (default: the model name)
#   --model-no-tools       the model cannot call tools: chat only, no files
#   --models-file FILE     a whole catalog instead (format: platform/models.toml), e.g. several models
#     --model-base-url / --models-file are remembered in platform/models.local.toml (not in git);
#     --models-file platform/models.toml goes back to the stock catalog.
#   Sign-up:
#   --registration MODE    open (anyone who can reach the Portal), invite (needs --invite-code) or closed
#   --invite-code CODE     the code new users must enter in invite mode
#   --skip-build       reuse the images already built/imported (config-only redeploy)
#   --no-portal        do not build/start the Portal (needs node/npm otherwise)
#   --listen ADDR      Portal bind address (default 127.0.0.1; 0.0.0.0 exposes it on the network - plain HTTP!)
#   --smoke-real       also run one short task on the real model (spends a few thousand tokens)
#   --dry-run          print what would be done, change nothing
#   -h, --help
#
# Safe to run repeatedly. It never prints a model key; the admin password is printed once at the end.
# Prerequisites it checks for you: docker, k3s (or kubectl + a kubeconfig), curl, openssl, python3, root.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PLATFORM=$ROOT/platform
NS=platform
PLATFORM_IMAGE=ailab-platform:lite
SANDBOX_IMAGE=${AILAB_SANDBOX_IMAGE:-harbor.ailab.local/sandbox/sbx-office:lite}
API_URL=http://127.0.0.1:30080
BROKER_URL=http://127.0.0.1:8081
OSB_URL=http://127.0.0.1:8090
PORTAL_PORT=3000

CMD=up MODEL_KEY='' SKIP_BUILD=0 WITH_PORTAL=1 LISTEN=127.0.0.1 SMOKE_REAL=0 DRY=0 YES=0
MODEL_BASE_URL='' MODEL_NAME='' MODEL_LABEL='' MODEL_NO_TOOLS=0 MODELS_FILE='' REGISTRATION='' INVITE_CODE=''
CATALOG='' KEY_ENV='' DEFAULT_MODEL=''
while [ $# -gt 0 ]; do
  case "$1" in
    up|status|down) CMD=$1 ;;
    --model-key) MODEL_KEY=${2:?--model-key needs a value}; shift ;;
    --model-key=*) MODEL_KEY=${1#*=} ;;
    --model-base-url) MODEL_BASE_URL=${2:?--model-base-url needs a value}; shift ;;
    --model-base-url=*) MODEL_BASE_URL=${1#*=} ;;
    --model-name) MODEL_NAME=${2:?--model-name needs a value}; shift ;;
    --model-name=*) MODEL_NAME=${1#*=} ;;
    --model-label) MODEL_LABEL=${2:?--model-label needs a value}; shift ;;
    --model-label=*) MODEL_LABEL=${1#*=} ;;
    --model-no-tools) MODEL_NO_TOOLS=1 ;;
    --models-file) MODELS_FILE=${2:?--models-file needs a value}; shift ;;
    --models-file=*) MODELS_FILE=${1#*=} ;;
    --registration) REGISTRATION=${2:?--registration needs open, invite or closed}; shift ;;
    --registration=*) REGISTRATION=${1#*=} ;;
    --invite-code) INVITE_CODE=${2:?--invite-code needs a value}; shift ;;
    --invite-code=*) INVITE_CODE=${1#*=} ;;
    --skip-build) SKIP_BUILD=1 ;;
    --no-portal) WITH_PORTAL=0 ;;
    --listen) LISTEN=${2:?--listen needs an address}; shift ;;
    --listen=*) LISTEN=${1#*=} ;;
    --smoke-real) SMOKE_REAL=1 ;;
    --dry-run) DRY=1 ;;
    --yes|-y) YES=1 ;;
    -h|--help) sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

# ---------------------------------------------------------------------------------------- output
if [ -t 1 ]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'; else B='' G='' Y='' R='' N=''; fi
STEP=0
step() { STEP=$((STEP + 1)); echo; echo "${B}[$STEP] $*${N}"; }
ok()   { echo "    ${G}✓${N} $*"; }
warn() { echo "    ${Y}!${N} $*"; }
die()  { echo; echo "${R}✗ $*${N}" >&2; exit 1; }
run()  { if [ "$DRY" = 1 ]; then echo "    (dry-run) $*" >&2; else "$@"; fi; }
JAR=
rc=0
trap 'rc=$?; [ -z "$JAR" ] || rm -f "$JAR"; [ $rc -eq 0 ] || echo "${R}deploy stopped at step $STEP (exit $rc). Fix the cause and run ./deploy.sh again: every step is safe to repeat.${N}" >&2' EXIT

# ------------------------------------------------------------------------------------- helpers
. "$PLATFORM/scripts/_common.sh" 2>/dev/null || true   # sets KUBECONFIG / KUBECTL when k3s is there

k() { $KUBECTL -n "$NS" "$@"; }
dev_token() { k get secret platform-secrets -o jsonpath='{.data.AILAB_DEV_TOKEN}' | base64 -d; }
admin_password() { k get secret platform-secrets -o jsonpath='{.data.AILAB_ADMIN_PASSWORD}' 2>/dev/null | base64 -d; }

wait_http() {  # url, seconds, label
  local url=$1 secs=$2 label=$3 i
  [ "$DRY" = 1 ] && { echo "    (dry-run) wait for $label"; return 0; }
  for i in $(seq 1 "$secs"); do
    curl -fsS -m 3 -o /dev/null "$url" 2>/dev/null && { ok "$label is up"; return 0; }
    sleep 1
  done
  return 1
}

portal_pid() {  # pid of whatever listens on the Portal port, if anything
  ss -ltnp 2>/dev/null | awk -v p=":$PORTAL_PORT" '$4 ~ p"$" {print $NF}' | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2
}

read_env_key() {  # $1 (an AILAB_KEY_* name) from the environment or platform/.env, without echoing it anywhere
  [ -n "$1" ] || return 0
  if [ -n "${!1:-}" ]; then printf %s "${!1}"; return 0; fi
  [ -f "$PLATFORM/.env" ] && grep -E "^$1=." "$PLATFORM/.env" | head -1 | cut -d= -f2- || true
}

# --------------------------------------------------------------------------------------- status
cmd_status() {
  echo "${B}AILab lite${N}"
  $KUBECTL -n "$NS" get pods 2>/dev/null || echo "  platform namespace not found (run ./deploy.sh)"
  echo
  for t in "API|$API_URL/readyz" "Broker|$BROKER_URL/readyz" "OpenSandbox|$OSB_URL/health" "Portal|http://127.0.0.1:$PORTAL_PORT/login"; do
    printf '  %-12s ' "${t%%|*}"
    if curl -fsS -m 3 -o /dev/null "${t#*|}" 2>/dev/null; then echo "${G}up${N}"; else echo "${R}down${N}"; fi
  done
  local pid; pid=$(portal_pid || true)
  echo "  Portal pid   ${pid:-none}"
  if tok=$(dev_token 2>/dev/null) && [ -n "$tok" ]; then
    echo; echo "  models users can pick:"
    curl -fsS -m 5 -H "Authorization: Bearer $tok" "$API_URL/v1/models" 2>/dev/null \
      | python3 -c 'import sys,json; d=json.load(sys.stdin); [print("   -", m["id"], "(", m["name"], ", x%s )" % m["weight"], "[default]" if m["default"] else "") for m in d["models"]]' 2>/dev/null \
      || echo "   (API not reachable)"
  fi
}

# ----------------------------------------------------------------------------------------- down
cmd_down() {
  if [ "$YES" != 1 ] && [ "$DRY" != 1 ]; then
    read -r -p "Delete the '$NS' namespace (database, files, runs) and stop the Portal? [y/N] " a
    [ "$a" = y ] || [ "$a" = Y ] || die "aborted"
  fi
  step "stopping the Portal"
  local pid; pid=$(portal_pid || true)
  if [ -n "$pid" ] && readlink "/proc/$pid/cwd" 2>/dev/null | grep -q "/portal$"; then run kill "$pid"; ok "stopped pid $pid"; else ok "not running"; fi
  step "removing the platform namespace"
  run $KUBECTL delete namespace "$NS" --ignore-not-found --wait=true
  ok "gone. Images, the OpenSandbox server and models.toml are untouched."
}

# ------------------------------------------------------------------------------------------- up
preflight() {
  step "checking prerequisites"
  local missing=() c
  for c in docker curl openssl python3 base64 ss; do command -v "$c" >/dev/null 2>&1 || missing+=("$c"); done
  if [ "$SKIP_BUILD" = 1 ]; then
    command -v k3s >/dev/null 2>&1 || command -v kubectl >/dev/null 2>&1 || missing+=("k3s (or kubectl)")
  else
    command -v k3s >/dev/null 2>&1 || missing+=("k3s (image import; use --skip-build if already imported)")
  fi
  [ "$WITH_PORTAL" = 1 ] && { command -v node >/dev/null 2>&1 || missing+=("node"); command -v npm >/dev/null 2>&1 || missing+=("npm"); }
  [ ${#missing[@]} -eq 0 ] || die "missing: ${missing[*]}  (install them, or use --no-portal if only node/npm are missing)"
  [ -n "${KUBECTL:-}" ] || die "no kubeconfig found (tried \$KUBECONFIG, ~/.kube/config, /etc/rancher/k3s/k3s.yaml): is k3s installed? see tests/t0_base.sh"
  [ "$(id -u)" = 0 ] || die "run as root (k3s imports images through containerd): sudo ./deploy.sh"
  docker info >/dev/null 2>&1 || die "the Docker daemon is not reachable"
  $KUBECTL get nodes --no-headers 2>/dev/null | grep -q ' Ready' || die "no Ready k3s node: $($KUBECTL get nodes --no-headers 2>&1 | head -2)"
  ok "docker, k3s node Ready, kubeconfig $KUBECONFIG"
  local avail; avail=$(awk '/MemAvailable/ {printf "%d", $2/1024}' /proc/meminfo)
  [ "${avail:-0}" -ge 2500 ] && ok "${avail} MB memory available" || warn "only ${avail} MB memory available; builds and sandboxes may be slow or fail"
  if [ "$WITH_PORTAL" = 1 ]; then
    local pid; pid=$(portal_pid || true)
    if [ -n "$pid" ] && ! readlink "/proc/$pid/cwd" 2>/dev/null | grep -q "/portal$"; then
      die "port $PORTAL_PORT is used by another program (pid $pid); free it or use --no-portal"
    fi
  fi
  resolve_models
  case "${REGISTRATION:-}" in
    ""|open|closed) ;;
    invite) [ -n "$INVITE_CODE" ] || die "--registration invite needs --invite-code" ;;
    *) die "--registration must be open, invite or closed" ;;
  esac
}

# Which catalog to publish, which key the default model needs, and a quick call to a custom endpoint.
resolve_models() {
  local mc="python3 $PLATFORM/scripts/make_catalog.py" local_cat="$PLATFORM/models.local.toml"
  if [ -n "$MODELS_FILE" ]; then
    [ -f "$MODELS_FILE" ] || die "--models-file: $MODELS_FILE not found"
    $mc check "$MODELS_FILE" >/dev/null || die "--models-file: not a valid catalog"
    if [ "$(cd "$(dirname "$MODELS_FILE")" && pwd)/$(basename "$MODELS_FILE")" = "$PLATFORM/models.toml" ]; then
      run rm -f "$local_cat"; ok "back to the stock catalog (platform/models.toml)"
    else
      run cp "$MODELS_FILE" "$local_cat"; ok "catalog from $MODELS_FILE (kept as platform/models.local.toml)"
    fi
  elif [ -n "$MODEL_BASE_URL" ]; then
    [ -n "$MODEL_NAME" ] || die "--model-base-url needs --model-name (the model as that API names it)"
    local args=(--base-url "$MODEL_BASE_URL" --name "$MODEL_NAME")
    [ -n "$MODEL_LABEL" ] && args+=(--label "$MODEL_LABEL")
    [ -n "$MODEL_KEY" ] && args+=(--key-env AILAB_KEY_MAIN)
    [ "$MODEL_NO_TOOLS" = 1 ] && args+=(--no-tools)
    [ "$DRY" = 1 ] && local_cat=$(mktemp --suffix=.toml)  # show what would be published, write nothing real
    ok "model $($mc custom "$local_cat" "${args[@]}") -> $MODEL_BASE_URL ($MODEL_NAME)$([ "$DRY" = 1 ] && echo ", would be saved in" || echo ", saved in") platform/models.local.toml"
  elif [ -n "$MODEL_NAME$MODEL_LABEL" ]; then
    die "--model-name / --model-label need --model-base-url"
  fi
  CATALOG=$PLATFORM/models.toml
  [ -f "$local_cat" ] && CATALOG=$local_cat
  $mc check "$CATALOG" >/dev/null || die "$CATALOG is not a valid catalog"
  KEY_ENV=$($mc key-env "$CATALOG")
  DEFAULT_MODEL=$($mc default "$CATALOG")
  ok "catalog $(basename "$CATALOG"): $($mc check "$CATALOG")  (default: $DEFAULT_MODEL)"
  [ -n "$MODEL_KEY" ] || MODEL_KEY=$(read_env_key "$KEY_ENV")
  if [ -z "$KEY_ENV" ] && [ -n "$MODEL_KEY" ]; then warn "--model-key given, but $DEFAULT_MODEL takes no key: pass --model-base-url and --model-name again together with --model-key"
  elif [ -z "$KEY_ENV" ]; then ok "the default model needs no key"
  elif [ -n "$MODEL_KEY" ]; then ok "key for $DEFAULT_MODEL found ($KEY_ENV)"
  else warn "no key for $DEFAULT_MODEL (--model-key, \$$KEY_ENV or platform/.env): it stays hidden; the offline demo model works"; fi

  if [ -n "$MODEL_BASE_URL" ] && [ "$DRY" != 1 ]; then  # catch a wrong URL / name / key now, not at the first chat
    local code body
    body=$(mktemp)
    code=$(curl -s -m 60 -o "$body" -w '%{http_code}' -H 'Content-Type: application/json' ${MODEL_KEY:+-H "Authorization: Bearer $MODEL_KEY"} \
      -d "$(python3 -c 'import json,sys; print(json.dumps({"model": sys.argv[1], "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}))' "$MODEL_NAME")" \
      "${MODEL_BASE_URL%/}/chat/completions" || true)
    if [ "$code" = 200 ]; then ok "the model answers ($MODEL_NAME)"
    else warn "test call to ${MODEL_BASE_URL%/}/chat/completions returned ${code:-no answer}: $(head -c 200 "$body" | tr '\n' ' ')"; fi
    rm -f "$body"
  fi
}

build_images() {
  step "sandbox image ($SANDBOX_IMAGE)"
  # The image carries a hash of images/sbx-office (Dockerfile, skills, smoke test): rebuild when the skills change.
  local want have
  want=$(cd "$ROOT/images/sbx-office" && find . -type f -not -path '*/__pycache__/*' -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16)
  have=$(docker image inspect --format '{{index .Config.Labels "ailab.content-hash"}}' "$SANDBOX_IMAGE" 2>/dev/null || true)
  if [ "$have" = "$want" ]; then ok "up to date ($want)"
  elif [ "$SKIP_BUILD" = 1 ]; then
    docker image inspect "$SANDBOX_IMAGE" >/dev/null 2>&1 || die "--skip-build but $SANDBOX_IMAGE does not exist"
    warn "out of date with images/sbx-office, kept because of --skip-build"
  else
    if docker image inspect "$SANDBOX_IMAGE" >/dev/null 2>&1; then ok "skills or Dockerfile changed: rebuilding (cached layers make this quick)"
    else warn "not found: building it (a few minutes, needs network for apt/pip)"; fi
    run nice -n 19 docker build -q --label "ailab.content-hash=$want" -t "$SANDBOX_IMAGE" "$ROOT/images/sbx-office" >/dev/null
    ok "built ($want)"
  fi

  step "platform image ($PLATFORM_IMAGE) -> k3s containerd"
  if [ "$SKIP_BUILD" = 1 ]; then ok "skipped (--skip-build)"; return; fi
  run nice -n 19 docker build -q -f "$PLATFORM/deploy/Dockerfile" -t "$PLATFORM_IMAGE" "$ROOT" >/dev/null
  [ "$DRY" = 1 ] && return
  local id; id=$(docker image inspect "$PLATFORM_IMAGE" --format '{{.Id}}'); id=${id#sha256:}
  docker save "$PLATFORM_IMAGE" | nice -n 19 k3s ctr -n k8s.io images import - >/dev/null
  # `docker save` can silently produce a broken archive, so check that k3s really holds this exact image
  # ctr images ls reports the manifest digest; Docker's .Id is the configuration digest. CRI exposes that
  # same configuration id, so compare like with like instead of rejecting every valid import.
  local imported; imported=$(k3s crictl inspecti "$PLATFORM_IMAGE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"]["id"])')
  [ "$imported" = "sha256:$id" ] || die "imported image does not match the one just built (${id:0:12}); not deploying a stale or broken image"
  ok "built and imported (${id:0:12})"
}

deploy_platform() {
  step "OpenSandbox server (sandbox control plane for k3s)"
  if curl -fsS -m 3 "$OSB_URL/health" >/dev/null 2>&1; then ok "already healthy on :8090"
  else run bash "$PLATFORM/scripts/osb_up.sh" | tail -2; fi

  step "secrets (random tokens; model key)"
  local existing=0; k get secret platform-secrets >/dev/null 2>&1 && existing=1
  if [ "$existing" = 0 ]; then
    if [ "$DRY" = 1 ]; then echo "    (dry-run) create platform-secrets with random tokens${MODEL_KEY:+ and the model key}"
    elif [ -n "$KEY_ENV" ] && [ -n "$MODEL_KEY" ]; then env "$KEY_ENV=$MODEL_KEY" bash "$PLATFORM/scripts/k8s_secrets.sh" >/dev/null; ok "created with random tokens and $KEY_ENV"
    else bash "$PLATFORM/scripts/k8s_secrets.sh" >/dev/null; ok "created with random tokens"; fi
  else
    ok "exists (tokens kept)"
    # secrets made before password login existed have no admin password yet
    if [ "$DRY" != 1 ] && [ -z "$(admin_password)" ]; then
      k patch secret platform-secrets --type merge -p "{\"data\":{\"AILAB_ADMIN_PASSWORD\":\"$(printf %s "$(openssl rand -hex 10)" | base64 -w0)\"}}" >/dev/null; ok "admin password generated"
    fi
    if [ -n "$KEY_ENV" ] && [ -n "$MODEL_KEY" ] && [ "$DRY" != 1 ]; then
      local have want; have=$(k get secret platform-secrets -o "jsonpath={.data.$KEY_ENV}" 2>/dev/null || true); want=$(printf %s "$MODEL_KEY" | base64 -w0)
      if [ "$have" != "$want" ]; then k patch secret platform-secrets --type merge -p "{\"data\":{\"$KEY_ENV\":\"$want\"}}" >/dev/null; ok "$KEY_ENV updated"; fi
    fi
  fi
  if [ -n "$INVITE_CODE" ] && [ "$DRY" != 1 ]; then
    k patch secret platform-secrets --type merge -p "{\"data\":{\"AILAB_INVITE_CODE\":\"$(printf %s "$INVITE_CODE" | base64 -w0)\"}}" >/dev/null
    ok "invite code set"
  fi

  step "model catalog ($(basename "$CATALOG"))"
  run env NO_RESTART=1 MODELS_FILE="$CATALOG" bash "$PLATFORM/scripts/k8s_models.sh" | tail -2

  step "sign-up"
  if [ -n "$REGISTRATION" ]; then
    run sh -c "$KUBECTL -n $NS create configmap platform-auth --from-literal=AILAB_REGISTRATION=$REGISTRATION --dry-run=client -o yaml | $KUBECTL apply -f - >/dev/null"
    ok "registration: $REGISTRATION"
  else
    ok "registration: $(k get configmap platform-auth -o jsonpath='{.data.AILAB_REGISTRATION}' 2>/dev/null || echo open) (unchanged; --registration to change)"
  fi

  step "deploying to k3s"
  local was_deployed=0; k get deploy platform-api >/dev/null 2>&1 && was_deployed=1
  local applied; applied=$(run $KUBECTL apply -f "$PLATFORM/deploy/k8s/platform.yaml") || die "could not apply the platform manifest"
  printf '%s\n' "$applied" | grep -v unchanged | sed 's/^/    /' || true
  if [ "$was_deployed" = 1 ]; then
    # Same image tag: restart to pick up new code/config - but only what apply left unchanged. Restarting a
    # deployment apply just changed starts a second generation of pods while the first is still coming up.
    for d in api broker agent worker; do
      printf '%s\n' "$applied" | grep -q "deployment.apps/platform-$d configured" && continue
      run $KUBECTL -n "$NS" rollout restart "deploy/platform-$d" >/dev/null
    done
  fi
  if [ "$DRY" != 1 ]; then
    for d in redis api broker agent worker; do k rollout status "deploy/platform-$d" --timeout=300s >/dev/null || die "deployment platform-$d did not become ready: $(k get pods | head -8)"; done
    ok "5 deployments ready"
    # Pods of the previous generation may still be shutting down; one of them must not take the smoke test's run.
    local i
    for i in $(seq 1 90); do
      $KUBECTL -n "$NS" get pods -o jsonpath='{range .items[*]}{.metadata.deletionTimestamp}{"\n"}{end}' | grep -q . || break
      sleep 1
    done
    ok "previous pods are gone"
  fi
  wait_http "$API_URL/readyz" 90 "Platform API (:30080)" || die "API not ready"
  wait_http "$BROKER_URL/readyz" 60 "Sandbox Broker (:8081)" || die "Broker not ready"
}

start_portal() {
  [ "$WITH_PORTAL" = 1 ] || { step "Portal"; ok "skipped (--no-portal)"; return; }
  step "Portal (Next.js, port $PORTAL_PORT, bound to $LISTEN)"
  cd "$PLATFORM/portal"
  local pid; pid=$(portal_pid || true)
  if [ -n "$pid" ]; then run kill "$pid"; [ "$DRY" = 1 ] || { for _ in $(seq 20); do [ -z "$(portal_pid || true)" ] && break; sleep 0.5; done; }; ok "stopped the previous Portal (pid $pid)"; fi
  # Reinstall whenever package-lock.json changed since the last install: after a dependency upgrade
  # (e.g. a Next.js major) building against the old node_modules would silently test and ship the old version.
  local lock_sum; lock_sum=$(sha256sum package-lock.json | cut -d' ' -f1)
  if [ ! -d node_modules ] || [ "$(cat node_modules/.ailab-lock-sha256 2>/dev/null)" != "$lock_sum" ]; then
    run npm ci --no-audit --no-fund >/dev/null
    [ "$DRY" = 1 ] || echo "$lock_sum" > node_modules/.ailab-lock-sha256
    ok "installed Portal dependencies from package-lock.json"
  else
    ok "Portal dependencies match package-lock.json"
  fi
  # a dev server and a production build share .next, so the old process had to go first
  run nice -n 19 npm run build >/dev/null
  mkdir -p "$PLATFORM/var/log" "$PLATFORM/var/run"
  if [ "$DRY" = 1 ]; then echo "    (dry-run) next start -H $LISTEN -p $PORTAL_PORT"; cd "$ROOT"; return; fi
  AILAB_API_BASE=$API_URL setsid nohup ./node_modules/.bin/next start -H "$LISTEN" -p "$PORTAL_PORT" > "$PLATFORM/var/log/portal.log" 2>&1 < /dev/null &
  wait_http "http://127.0.0.1:$PORTAL_PORT/login" 60 "Portal" || { tail -20 "$PLATFORM/var/log/portal.log"; die "Portal did not start"; }
  portal_pid > "$PLATFORM/var/run/portal.pid" || true
  cd "$ROOT"
  [ "$LISTEN" = 127.0.0.1 ] || warn "the Portal listens on $LISTEN over plain HTTP: the password and session cookie travel unencrypted; put TLS in front before real use"
}

smoke_test() {
  step "smoke test"
  [ "$DRY" = 1 ] && { echo "    (dry-run) log in through the Portal, run a task on the offline model"; return; }
  local tok base=$API_URL; tok=$(dev_token); JAR=$(mktemp)

  [ "$(curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer dev-demo-token' "$base/v1/runs")" = 401 ] && ok "the published default token is rejected" || die "the default dev token still works"
  if [ "$WITH_PORTAL" = 1 ]; then
    local p=http://127.0.0.1:$PORTAL_PORT
    [ "$(curl -s -o /dev/null -w '%{http_code}' "$p/api/v1/runs")" = 401 ] && ok "Portal API proxy refuses anonymous requests" || die "Portal proxy is open to anonymous requests"
    [ "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d "{\"username\":\"admin\",\"password\":\"$tok\"}" "$p/api/auth/login")" = 401 ] && ok "the API token is not accepted as a password" || die "the API token works as a Portal password"
    curl -fsS -c "$JAR" -X POST -H 'Content-Type: application/json' -d "{\"username\":\"admin\",\"password\":\"$(admin_password)\"}" "$p/api/auth/login" >/dev/null || die "login through the Portal failed"
    grep -q HttpOnly "$JAR" && ok "login works; session cookie is httpOnly" || die "session cookie is not httpOnly"
    curl -fsS -b "$JAR" "$p/api/v1/models" | grep -q '"models"' && ok "models are listed through the Portal proxy" || die "models not reachable through the Portal"
  fi

  local models; models=$(curl -fsS -H "Authorization: Bearer $tok" "$base/v1/models" | python3 -c 'import sys,json; print(" ".join(m["id"] for m in json.load(sys.stdin)["models"]))')
  ok "selectable models: $models"
  smoke_run() {  # model_id prompt seconds
    local rid st i
    rid=$(curl -fsS -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' -d "{\"prompt\":\"$2\",\"model\":\"$1\"}" "$base/v1/runs" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])') || die "could not create a run on $1"
    for i in $(seq 1 "$3"); do
      st=$(curl -fsS -H "Authorization: Bearer $tok" "$base/v1/runs/$rid" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["status"], d["error"] or "")')
      case "$st" in succeeded*) ok "task on $1 succeeded (${i}s)"; return 0 ;; failed*|canceled*) die "task on $1 ended: $st" ;; esac
      sleep 1
    done
    die "task on $1 did not finish in $3 s (run $rid)"
  }
  case " $models " in *" ailab-demo "*) smoke_run ailab-demo "你好，在沙箱里执行一条命令" 120 ;; esac
  if [ "$SMOKE_REAL" = 1 ]; then
    case " $models " in
      *" $DEFAULT_MODEL "*) [ "$DEFAULT_MODEL" = ailab-demo ] || smoke_run "$DEFAULT_MODEL" "在沙箱里执行 echo deploy-ok，然后一句话告诉我输出" 240 ;;
      *) warn "$DEFAULT_MODEL unavailable (no key): skipping the real-model task" ;;
    esac
  fi
}

summary() {
  local pw; pw=$(admin_password 2>/dev/null || echo "<see below>")
  echo
  echo "${G}${B}AILab lite is up.${N}"
  [ "$WITH_PORTAL" = 1 ] && echo "  Portal        http://$([ "$LISTEN" = 0.0.0.0 ] && hostname -I | awk '{print $1}' || echo "$LISTEN"):$PORTAL_PORT   (log in with the account below)"
  echo "  API / docs    $API_URL/docs"
  echo "  login         user  admin"
  echo "                pass  $pw"
  echo "                (again later:  $KUBECTL -n $NS get secret platform-secrets -o jsonpath='{.data.AILAB_ADMIN_PASSWORD}' | base64 -d)"
  echo "  more users    $KUBECTL -n $NS exec -it deploy/platform-api -- python -m ailab_platform.users add NAME"
  [ -z "$KEY_ENV" ] || [ -n "$MODEL_KEY" ] || echo "  ${Y}no model key: add one with  ./deploy.sh --skip-build --no-portal --model-key sk-...${N}"
  echo "  sign-up       $(k get configmap platform-auth -o jsonpath='{.data.AILAB_REGISTRATION}' 2>/dev/null || echo open) (users register on the login page; --registration open|invite|closed)"
  echo
  echo "  status:    ./deploy.sh status        remove:  ./deploy.sh down"
  echo "  Portal only listens on this machine; from your laptop:  ssh -L $PORTAL_PORT:127.0.0.1:$PORTAL_PORT <this-host>"
  echo "  demo:      platform/scripts/showcase_doc.py  (upload a document, get a deck)   docs: platform/README.md"
}

case "$CMD" in
  status) cmd_status ;;
  down) cmd_down ;;
  up)
    [ "$DRY" = 1 ] && echo "${Y}dry-run: nothing will be changed${N}"
    preflight
    build_images
    deploy_platform
    start_portal
    smoke_test
    [ "$DRY" = 1 ] || summary
    ;;
esac
