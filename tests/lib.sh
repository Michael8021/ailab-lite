#!/usr/bin/env bash
# 测试脚本公共函数（source 使用）
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
LOG_DIR=$ROOT/logs
mkdir -p "$LOG_DIR"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
export K="k3s kubectl"   # 用 k3s 自带的同版本 kubectl（本机 /usr/bin/kubectl 是 1.37，和 1.34 服务端差 3 个版本）

log()  { echo "[$(date -u +%H:%M:%S)] $*"; }
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; exit 1; }

# ---- 宿主机保护钩子 ------------------------------------------------------------------------------
# 这些脚本会装 k3s、改容器运行时，在一台还跑着别的重要服务的机器上要小心。默认什么都不拦；
# 想加保护，把下面两个函数在 tests/local.sh（已被 gitignore）里重新定义，样例见 tests/local.sh.example：
#   guard_busy_window  忙的时候 `fail "..."` 拒绝继续（FORCE=1 可强制）
#   host_probe         打印一行本机关键服务的健康状况，不健康时返回非零；脚本会在改动前后各调用一次
guard_busy_window() { :; }
host_probe() { :; }
# (an if, not `[ -f ] && source`: with set -e the latter would end the caller when local.sh is absent)
if [ -f "$ROOT/tests/local.sh" ]; then source "$ROOT/tests/local.sh"; fi
