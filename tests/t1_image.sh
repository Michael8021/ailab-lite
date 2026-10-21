#!/bin/bash
# T1 沙箱镜像：构建 → docker 内冒烟（runc，断网）→ 导入 k3s → gVisor Pod 内冒烟（NetworkPolicy 断网）。
# 验收：两种运行时下 SMOKE PASS；中文字体映射生效；溢出检查能发现问题；网络全部不通；记录内存峰值和渲染耗时。
source "$(dirname "$0")/lib.sh"
IMG=harbor.ailab.local/sandbox/sbx-office:lite   # 与大集群同名，靠预导入而不是拉取
guard_busy_window

log "1/4 构建（amd64；arm64 需 QEMU 模拟，较慢，放到 T1b 单独做）"
docker build -t "$IMG" "$ROOT/images/sbx-office" 2>&1 | tail -5
echo "  $(docker image inspect "$IMG" --format '{{.Architecture}} {{.Size}}' | awk '{printf "%s %.0fMB", $1, $2/1048576}')"

log "2/4 docker（runc）内冒烟"
docker run --rm --network none --memory 2g --cpus 1 "$IMG" sh /opt/ailab-tools/smoke/smoke.sh \
  | tee "$LOG_DIR/t1-docker.log" | tail -12 || true
grep -q 'SMOKE PASS' "$LOG_DIR/t1-docker.log" || fail "docker 冒烟失败"
grep -q 'NET_OPEN' "$LOG_DIR/t1-docker.log" && fail "docker --network none 下网络居然通了"
pass "runc 冒烟"

log "3/4 导入 k3s（docker save 可能静默产出坏包，导入后必须核对）"
docker save "$IMG" | k3s ctr -n k8s.io images import -
k3s ctr -n k8s.io images ls -q | grep -qx "$IMG" || fail "导入后 k3s 里找不到镜像"

log "4/4 gVisor Pod 内冒烟（sandbox 命名空间：配额 1 个 Pod、默认拒绝所有网络）"
$K -n sandbox delete pod sbx-smoke --ignore-not-found --wait
cat <<EOF | $K apply -f -
apiVersion: v1
kind: Pod
metadata: { name: sbx-smoke, namespace: sandbox }
spec:
  runtimeClassName: gvisor
  restartPolicy: Never
  automountServiceAccountToken: false
  activeDeadlineSeconds: 600
  securityContext: { runAsNonRoot: true, runAsUser: 10001 }
  containers:
  - name: sandbox
    image: $IMG
    imagePullPolicy: Never
    command: ["sh", "/opt/ailab-tools/smoke/smoke.sh"]
    resources:
      requests: { cpu: 250m, memory: 1536Mi }
      limits:   { cpu: "1",  memory: 1536Mi }
    securityContext: { allowPrivilegeEscalation: false, capabilities: { drop: [ALL] } }
EOF

log "配额检查：sbx-smoke 还在运行时，第 2 个 Pod 应被拒（已结束的 Pod 不计入配额，所以必须趁它在跑时测）"
out=$($K -n sandbox run quota-probe --image="$IMG" --image-pull-policy=Never --restart=Never \
        --overrides='{"spec":{"runtimeClassName":"gvisor"}}' -- true 2>&1 || true)
echo "  $out"
if echo "$out" | grep -q 'exceeded quota'; then pass "ResourceQuota pods=1 生效"
else $K -n sandbox delete pod quota-probe --ignore-not-found; fail "第 2 个沙箱没被配额拦住"; fi

$K -n sandbox wait --for=jsonpath='{.status.phase}'=Succeeded pod/sbx-smoke --timeout=600s \
  || { $K -n sandbox describe pod sbx-smoke | tail -20; $K -n sandbox logs sbx-smoke | tail -30; fail "gVisor 冒烟没跑完"; }
$K -n sandbox logs sbx-smoke | tee "$LOG_DIR/t1-gvisor.log" | tail -12
grep -q 'SMOKE PASS' "$LOG_DIR/t1-gvisor.log" || fail "gVisor 冒烟失败"
grep -q 'NET_OPEN' "$LOG_DIR/t1-gvisor.log" && fail "NetworkPolicy 没生效：沙箱能连外部"
pass "gVisor 冒烟（LibreOffice 可用、网络被拒）"

$K -n sandbox delete pod sbx-smoke --wait=false
pass "T1 完成"
