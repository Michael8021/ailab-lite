#!/bin/sh
# 沙箱镜像冒烟测试：在容器内执行，任何一步失败即退出非 0。
set -eu
cd /workspace
echo "== whoami: $(id -u)"
python -c "import pptx, docx, openpyxl, matplotlib, pandas, pypdf; print('py libs OK')"
fc-match "微软雅黑" | grep -q "Noto Sans CJK" || { echo "font map FAILED"; exit 1; }
echo "font map OK"

cat > /workspace/outline.json <<'EOF'
{"title": "AILab 简易版冒烟测试", "subtitle": "gVisor 沙箱",
 "slides": [
  {"title": "第一页：中文排版", "bullets": ["微软雅黑映射到 Noto Sans CJK", "LibreOffice 渲染预览"]},
  {"title": "第二页：故意溢出", "bullets": ["这是一条非常非常长的要点，用来测试溢出检查是否能够发现文字超出文本框的情况，重复一遍：这是一条非常非常长的要点，用来测试溢出检查是否能够发现文字超出文本框的情况", "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m", "n", "o", "p", "q", "r", "s", "t"]}
 ]}
EOF
python /opt/skills/pptx-builder/scripts/build.py /workspace/outline.json /workspace/outputs/smoke.pptx
start=$(date +%s)
python /opt/skills/pptx-builder/scripts/preview.py /workspace/outputs/smoke.pptx | tee /tmp/preview.log
echo "render seconds: $(( $(date +%s) - start ))"
test "$(grep -c '^PNG ' /tmp/preview.log)" -eq 3
grep -q "OVERFLOW slide=3" /tmp/preview.log || { echo "overflow check FAILED"; exit 1; }
echo "overflow check OK"
# 网络应当完全不通（docker 用 --network none，k8s 靠 NetworkPolicy）
python - <<'PY'
import socket
for host, port in [("pypi.org", 443), ("kubernetes.default.svc", 443), ("10.43.0.1", 443)]:
    try:
        socket.create_connection((host, port), timeout=5).close()
        print(f"NET_OPEN {host}:{port}")
    except OSError as e:
        print(f"net blocked {host}:{port} ({type(e).__name__})")
PY
for f in /sys/fs/cgroup/memory.peak /sys/fs/cgroup/memory/memory.max_usage_in_bytes; do
  [ -r "$f" ] && echo "memory peak MiB: $(( $(cat $f) / 1048576 ))" && break
done || true
echo "SMOKE PASS"
