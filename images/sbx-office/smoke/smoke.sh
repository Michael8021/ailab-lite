#!/bin/sh
# 沙箱镜像冒烟测试：在容器内执行，任何一步失败即退出非 0。
set -eu
cd /workspace
echo "== whoami: $(id -u)"
python -c "import pptx, docx, openpyxl, matplotlib, pandas, pypdf; print('py libs OK')"
fc-match "微软雅黑" | grep -q "Noto Sans CJK" || { echo "font map FAILED"; exit 1; }
echo "font map OK"

mkdir -p /workspace/work
python /opt/skills/pptx/scripts/build.py --example > /workspace/work/deck.json
python - <<'PY'
import json
spec = json.load(open("/workspace/work/deck.json"))
for s in spec["slides"]:
    s.pop("image", None)  # no network, no pictures in the smoke test
spec["slides"].append({"layout": "bullets", "title": "故意过长", "bullets": ["这是一条非常非常长的要点，用来测试文字过长时的告警" * 6] * 6})
json.dump(spec, open("/workspace/work/deck.json", "w"), ensure_ascii=False)
PY
python /opt/skills/pptx/scripts/build.py /workspace/work/deck.json /workspace/outputs/smoke.pptx | tee /tmp/build.log
grep -q "^WARN slide 10" /tmp/build.log || { echo "too-long text was not reported"; exit 1; }
echo "overflow warning OK"
python /opt/skills/xlsx/scripts/build_xlsx.py --example > /workspace/work/book.json
python /opt/skills/xlsx/scripts/build_xlsx.py /workspace/work/book.json /workspace/outputs/smoke.xlsx
start=$(date +%s)
soffice --headless --convert-to pdf --outdir /tmp /workspace/outputs/smoke.pptx >/dev/null
pdftoppm -png -r 30 /tmp/smoke.pdf /tmp/smoke
echo "render seconds: $(( $(date +%s) - start ))"
test "$(ls /tmp/smoke-*.png | wc -l)" -eq 10
echo "render OK"
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
