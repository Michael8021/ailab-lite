#!/usr/bin/env bash
# One-command deployment of AILab lite on a machine that already runs k3s + Docker.
#
#   ./deploy.sh                       build everything, deploy to k3s, start the Portal, smoke-test it
#   ./deploy.sh status                what is running and healthy
#   ./deploy.sh down [--yes]          remove the platform from k3s (and stop the Portal)
#
# Options (for `up`):
#   --model-key KEY    API key for the "AILab 旗舰" model (DeepSeek). Also read from $AILAB_KEY_DEEPSEEK or
#                      platform/.env. Without a key only the offline demo model is selectable.
#   --skip-build       reuse the images already built/imported (config-only redeploy)
#   --no-portal        do not build/start the Portal (needs node/npm otherwise)
#   --listen ADDR      Portal bind address (default 127.0.0.1; 0.0.0.0 exposes it on the network - plain HTTP!)
#   --smoke-real       also run one short task on the real model (spends a few thousand tokens)
#   --dry-run          print what would be done, change nothing
#   -h, --help
#
# Safe to run repeatedly. It never prints a model key; the login token is printed once at the end.
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

CMD=up MODEL_KEY=${AILAB_KEY_DEEPSEEK:-} SKIP_BUILD=0 WITH_PORTAL=1 LISTEN=127.0.0.1 SMOKE_REAL=0 DRY=0 YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    up|status|down) CMD=$1 ;;
    --model-key) MODEL_KEY=${2:?--model-key needs a value}; shift ;;
    --model-key=*) MODEL_KEY=${1#*=} ;;
    --skip-build) SKIP_BUILD=1 ;;
    --no-portal) WITH_PORTAL=0 ;;
    --listen) LISTEN=${2:?--listen needs an address}; shift ;;
    --listen=*) LISTEN=${1#*=} ;;
    --smoke-real) SMOKE_REAL=1 ;;
    --dry-run) DRY=1 ;;
    --yes|-y) YES=1 ;;
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

read_env_key() {  # AILAB_KEY_DEEPSEEK from platform/.env, without echoing it anywhere
  [ -f "$PLATFORM/.env" ] && grep -E '^AILAB_KEY_DEEPSEEK=.' "$PLATFORM/.env" | head -1 | cut -d= -f2- || true
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
  [ -n "$MODEL_KEY" ] || MODEL_KEY=$(read_env_key)
  if [ -n "$MODEL_KEY" ]; then ok "model key found: the flagship model will be available"
  else warn "no model key (--model-key, \$AILAB_KEY_DEEPSEEK or platform/.env): only the offline demo model will be selectable"; fi
}

build_images() {
  step "sandbox image ($SANDBOX_IMAGE)"
  if docker image inspect "$SANDBOX_IMAGE" >/dev/null 2>&1; then ok "present"
  elif [ "$SKIP_BUILD" = 1 ]; then die "--skip-build but $SANDBOX_IMAGE does not exist"
  else warn "not found: building it (a few minutes, needs network for apt/pip)"; run docker build -t "$SANDBOX_IMAGE" "$ROOT/images/sbx-office"; fi

  step "platform image ($PLATFORM_IMAGE) -> k3s containerd"
  if [ "$SKIP_BUILD" = 1 ]; then ok "skipped (--skip-build)"; return; fi
  run nice -n 19 docker build -q -f "$PLATFORM/deploy/Dockerfile" -t "$PLATFORM_IMAGE" "$ROOT" >/dev/null
  [ "$DRY" = 1 ] && return
  local id; id=$(docker image inspect "$PLATFORM_IMAGE" --format '{{.Id}}'); id=${id#sha256:}
  docker save "$PLATFORM_IMAGE" | nice -n 19 k3s ctr -n k8s.io images import - >/dev/null
  # `docker save` can silently produce a broken archive, so check that k3s really holds this exact image
  k3s ctr -n k8s.io images ls | grep "$PLATFORM_IMAGE" | grep -q "$id" || die "imported image does not match the one just built (${id:0:12}); not deploying a stale or broken image"
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
    else AILAB_KEY_DEEPSEEK=$MODEL_KEY bash "$PLATFORM/scripts/k8s_secrets.sh" >/dev/null; ok "created with random tokens"; fi
  else
    ok "exists (tokens kept)"
    if [ -n "$MODEL_KEY" ] && [ "$DRY" != 1 ]; then
      local have want; have=$(k get secret platform-secrets -o jsonpath='{.data.AILAB_KEY_DEEPSEEK}' 2>/dev/null || true); want=$(printf %s "$MODEL_KEY" | base64 -w0)
      if [ "$have" != "$want" ]; then k patch secret platform-secrets --type merge -p "{\"data\":{\"AILAB_KEY_DEEPSEEK\":\"$want\"}}" >/dev/null; ok "model key updated"; fi
    fi
  fi

  step "model catalog (platform/models.toml)"
  run env NO_RESTART=1 bash "$PLATFORM/scripts/k8s_models.sh" | tail -2

  step "deploying to k3s"
  local was_deployed=0; k get deploy platform-api >/dev/null 2>&1 && was_deployed=1
  run $KUBECTL apply -f "$PLATFORM/deploy/k8s/platform.yaml" | grep -v unchanged || true
  if [ "$was_deployed" = 1 ]; then
    for d in api broker agent worker; do run $KUBECTL -n "$NS" rollout restart "deploy/platform-$d" >/dev/null; done  # same image tag: restart to pick up new code/config
  fi
  if [ "$DRY" != 1 ]; then
    for d in redis api broker agent worker; do k rollout status "deploy/platform-$d" --timeout=300s >/dev/null || die "deployment platform-$d did not become ready: $(k get pods | head -8)"; done
    ok "5 deployments ready"
  fi
  wait_http "$API_URL/readyz" 90 "Platform API (:30080)" || die "API not ready"
  wait_http "$BROKER_URL/readyz" 60 "Sandbox Broker (:8081)" || die "Broker not ready"
}

start_portal() {
  [ "$WITH_PORTAL" = 1 ] || { step "Portal"; ok "skipped (--no-portal)"; return; }
  step "Portal (Next.js, port $PORTAL_PORT, bound to $LISTEN)"
  cd "$PLATFORM/portal"
  [ -d node_modules ] || run npm ci --no-audit --no-fund >/dev/null
  local pid; pid=$(portal_pid || true)
  if [ -n "$pid" ]; then run kill "$pid"; [ "$DRY" = 1 ] || { for _ in $(seq 20); do [ -z "$(portal_pid || true)" ] && break; sleep 0.5; done; }; ok "stopped the previous Portal (pid $pid)"; fi
  # a dev server and a production build share .next, so the old process had to go first
  run nice -n 19 npm run build >/dev/null
  mkdir -p "$PLATFORM/var/log" "$PLATFORM/var/run"
  if [ "$DRY" = 1 ]; then echo "    (dry-run) next start -H $LISTEN -p $PORTAL_PORT"; cd "$ROOT"; return; fi
  AILAB_API_BASE=$API_URL setsid nohup ./node_modules/.bin/next start -H "$LISTEN" -p "$PORTAL_PORT" > "$PLATFORM/var/log/portal.log" 2>&1 < /dev/null &
  wait_http "http://127.0.0.1:$PORTAL_PORT/login" 60 "Portal" || { tail -20 "$PLATFORM/var/log/portal.log"; die "Portal did not start"; }
  portal_pid > "$PLATFORM/var/run/portal.pid" || true
  cd "$ROOT"
  [ "$LISTEN" = 127.0.0.1 ] || warn "the Portal listens on $LISTEN over plain HTTP: the session cookie and token travel unencrypted; put TLS in front before real use"
}

smoke_test() {
  step "smoke test"
  [ "$DRY" = 1 ] && { echo "    (dry-run) log in through the Portal, run a task on the offline model"; return; }
  local tok base=$API_URL; tok=$(dev_token); JAR=$(mktemp)

  [ "$(curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer dev-demo-token' "$base/v1/runs")" = 401 ] && ok "the published default token is rejected" || die "the default dev token still works"
  if [ "$WITH_PORTAL" = 1 ]; then
    local p=http://127.0.0.1:$PORTAL_PORT
    [ "$(curl -s -o /dev/null -w '%{http_code}' "$p/api/v1/runs")" = 401 ] && ok "Portal API proxy refuses anonymous requests" || die "Portal proxy is open to anonymous requests"
    curl -fsS -c "$JAR" -X POST -H 'Content-Type: application/json' -d "{\"token\":\"$tok\"}" "$p/api/auth/login" >/dev/null || die "login through the Portal failed"
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
    case " $models " in *" ailab-agent-pro "*) smoke_run ailab-agent-pro "在沙箱里执行 echo deploy-ok，然后一句话告诉我输出" 240 ;; *) warn "flagship model unavailable (no key): skipping the real-model task" ;; esac
  fi
}

summary() {
  local tok; tok=$(dev_token 2>/dev/null || echo "<see below>")
  echo
  echo "${G}${B}AILab lite is up.${N}"
  [ "$WITH_PORTAL" = 1 ] && echo "  Portal        http://$([ "$LISTEN" = 0.0.0.0 ] && hostname -I | awk '{print $1}' || echo "$LISTEN"):$PORTAL_PORT   (log in with the token below)"
  echo "  API / docs    $API_URL/docs"
  echo "  login token   $tok"
  echo "                (again later:  $KUBECTL -n $NS get secret platform-secrets -o jsonpath='{.data.AILAB_DEV_TOKEN}' | base64 -d)"
  [ -n "$MODEL_KEY" ] || echo "  ${Y}no model key: add one with  ./deploy.sh --skip-build --no-portal --model-key sk-...${N}"
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
