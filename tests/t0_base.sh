#!/bin/bash
# T0 基座：k3s v1.34 + gVisor RuntimeClass。
# 验收：gVisor Pod 能跑、确实在 gVisor 里；装前装后宿主机探针（tests/local.sh 的 host_probe，默认不检查）都正常；iptables 变更留档。
# 回滚：/usr/local/bin/k3s-uninstall.sh && dpkg -r runsc
source "$(dirname "$0")/lib.sh"
K3S_VERSION=v1.34.12+k3s1
RUNSC_DEB=/tmp/gv-deb/runsc.deb
RUNSC_SHA=7a440a035128a52f489b0bb76f60ac4df9cabe7cdf31dd5fd79a3e80f8a707ca

guard_busy_window
mkdir -p "$LOG_DIR"

log "装前：宿主机探针"
before=$(host_probe) || { echo "  $before"; fail "装前宿主机探针就不正常，先不动"; }
[ -z "$before" ] || echo "  $before"
iptables-save > "$LOG_DIR/iptables.before"

log "1/4 安装 gVisor（runsc）"
if ! command -v runsc >/dev/null; then
  [ -e /etc/docker/daemon.json ] && fail "/etc/docker/daemon.json 存在，runsc 的 postinst 会改 docker 配置并 reload；先人工确认"
  echo "$RUNSC_SHA  $RUNSC_DEB" | sha256sum -c - >/dev/null || fail "runsc.deb 校验失败"
  dpkg -i "$RUNSC_DEB"
fi
runsc --version | head -1

log "2/4 安装 k3s $K3S_VERSION"
if ! command -v k3s >/dev/null; then
  install -D -m 0644 "$ROOT/k3s/config.yaml" /etc/rancher/k3s/config.yaml
  mkdir -p /tmp/k3s-dl
  curl -sfL https://get.k3s.io -o /tmp/k3s-dl/install.sh
  INSTALL_K3S_VERSION=$K3S_VERSION sh /tmp/k3s-dl/install.sh
fi
for _ in $(seq 1 60); do $K get node 2>/dev/null | grep -q ' Ready' && break; sleep 2; done
$K get node -o wide

log "3/4 注册 runsc 到 k3s 的 containerd"
CDIR=/var/lib/rancher/k3s/agent/etc/containerd
# 用 k3s 生成配置里自带的 drop-in 目录（config-v3.toml.d/*.toml）；
# 不用 config-v3.toml.tmpl：在 k3s 1.34 上会把 base 渲染两遍，containerd 报 "table grpc already exists" 起不来
if ! k3s crictl info 2>/dev/null | grep -q '"runsc"'; then
  install -D -m 0644 "$ROOT/k3s/runsc.toml" "$CDIR/config-v3.toml.d/runsc.toml"
  systemctl restart k3s
  for _ in $(seq 1 60); do $K get node 2>/dev/null | grep -q ' Ready' && break; sleep 2; done
fi
k3s crictl info | grep -q '"runsc"' && pass "containerd 已注册 runsc" || fail "containerd 没有 runsc 运行时"
$K apply -k "$ROOT/deploy/overlays/lite"

log "4/4 gVisor Pod 验证"
$K -n default delete pod gvisor-probe --ignore-not-found --wait
$K -n default run gvisor-probe --image=busybox:1.37 --restart=Never \
  --overrides='{"spec":{"runtimeClassName":"gvisor"}}' -- sh -c 'dmesg | head -3; uname -r'
$K -n default wait --for=jsonpath='{.status.phase}'=Succeeded pod/gvisor-probe --timeout=180s
out=$($K -n default logs gvisor-probe)
echo "$out" | sed 's/^/  /'
echo "$out" | grep -qi gvisor && pass "Pod 运行在 gVisor 里" || fail "dmesg 里没有 gVisor 标记"
$K -n default delete pod gvisor-probe --wait=false

log "装后：iptables 差异与宿主机探针"
iptables-save > "$LOG_DIR/iptables.after"
diff <(grep -v '^#' "$LOG_DIR/iptables.before" | grep -E '^-A (FORWARD|DOCKER)' ) \
     <(grep -v '^#' "$LOG_DIR/iptables.after"  | grep -E '^-A (FORWARD|DOCKER)' ) \
  && echo "  docker 相关链无变化" || echo "  ↑ docker/FORWARD 链有变化，已留档 logs/iptables.*"
after=$(host_probe) && pass "宿主机探针正常${after:+：$after}" || fail "宿主机探针异常！立即执行回滚（${after:-无输出}）"
free -h | head -2
pass "T0 完成"
