"""Document -> PPT demo: upload a file, wait for the scan, let the agent read it and build a deck.

Usage (k3s):  eval "$(bash scripts/k8s_secrets.sh env)"
              .venv/bin/python scripts/showcase_doc.py path/to/document.md
Downloads the produced files to <repo>/demo-output/.
"""

import os
import sys
import time
from pathlib import Path

import httpx

B = os.environ.get("AILAB_API_BASE_URL", "http://127.0.0.1:30080").rstrip("/")
H = {"Authorization": f"Bearer {os.environ['AILAB_DEV_TOKEN']}"}
OUT = Path(__file__).resolve().parents[2] / "demo-output"
doc = sys.argv[1]
with httpx.Client(timeout=60) as c:
    up = c.post(f"{B}/v1/files", headers=H, files={"upload": (os.path.basename(doc), open(doc, "rb").read(), "text/markdown")}).json()
    print("uploaded:", up["filename"], up["size"], "bytes, status =", up["status"])
    for _ in range(60):
        f = c.get(f"{B}/v1/files/{up['id']}", headers=H).json()
        if f["status"] != "pending":
            break
        time.sleep(0.5)
    print("after scan: status =", f["status"], "| extract =", f["meta"].get("extract"))
    run = c.post(
        f"{B}/v1/runs",
        headers=H,
        json={
            "prompt": (
                "阅读我上传的这份设计文档，做一个 6 页的汇报 PPT：背景与目标、"
                "关键决策（每个决策一句话说清取舍）、主要风险、下一步。用中文，要点精炼。"
            ),
            "file_ids": [up["id"]],
        },
    ).json()
    print("run:", run["id"])
    seen = 0
    for _ in range(600):
        d = c.get(f"{B}/v1/runs/{run['id']}", headers=H).json()
        ev = c.get(f"{B}/v1/runs/{run['id']}/events/history", headers=H, params={"after": seen}).json()
        for e in ev:
            seen = e["seq"]
            if e["type"] in ("agent.message", "agent.tool_call", "artifact.created", "run.status"):
                p = e["payload"]
                note = p.get("content") or p.get("name") or p.get("filename") or p.get("status") or ""
                print(f"  [{e['seq']:>2}] {e['type']:<17} {str(note)[:90]}")
        if d["status"] in ("succeeded", "failed", "canceled"):
            break
        time.sleep(2)
    print("FINAL:", d["status"], d["error"] or "", "| usage", d["usage"])
    for a in d["artifacts"]:
        data = c.get(f"{B}{a['download_url']}", headers=H).content
        OUT.mkdir(exist_ok=True)
        path = OUT / a["filename"]
        path.write_bytes(data)
        print("saved:", path, len(data), "bytes")
