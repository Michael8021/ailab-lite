"""End-to-end demo: create a run, stream events, download the produced pptx.

Usage: platform/.venv/bin/python platform/scripts/e2e_demo.py ["prompt"]
"""

from __future__ import annotations

import os
import sys
import time

import httpx

BASE = os.environ.get("AILAB_API_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
TOKEN = os.environ.get("AILAB_DEV_TOKEN", "dev-demo-token")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
PROMPT = sys.argv[1] if len(sys.argv) > 1 else "帮我做一个 PPT，介绍 AILab 平台的分层架构"


def main() -> int:
    with httpx.Client(timeout=60) as client:
        resp = client.post(f"{BASE}/v1/runs", headers=HEADERS, json={"prompt": PROMPT})
        if resp.status_code >= 400:
            print("create run failed:", resp.status_code, resp.text)
            return 1
        run = resp.json()
        run_id = run["id"]
        print(f"run={run_id} status={run['status']}")

        seen = 0
        deadline = time.time() + 300
        while time.time() < deadline:
            detail = client.get(f"{BASE}/v1/runs/{run_id}", headers=HEADERS).json()
            events = client.get(f"{BASE}/v1/runs/{run_id}/events/history", headers=HEADERS, params={"after": seen}).json()
            for event in events:
                seen = max(seen, event["seq"])
                payload = event.get("payload") or {}
                brief = payload.get("content") or payload.get("name") or payload.get("status") or ""
                print(f"  [{event['seq']:>2}] {event['type']:<18} {str(brief)[:90]}")
            if detail["status"] in {"succeeded", "failed", "canceled"}:
                run = detail
                break
            time.sleep(0.5)
        else:
            print("timeout waiting for run")
            return 1

    print(f"\nfinal status: {run['status']}")
    print("summary:", (run.get("summary") or "")[:500])
    if run["status"] != "succeeded":
        print("error:", run.get("error"))
        return 1

    artifacts = run.get("artifacts") or []
    if not artifacts:
        print("no artifact produced")
        return 1
    for art in artifacts:
        url = BASE + art["download_url"]
        with httpx.Client(timeout=120) as client:
            data = client.get(url, headers=HEADERS).content
        magic = data[:4]
        print(f"artifact {art['filename']} {len(data)} bytes magic={magic!r}")
        if art["filename"].endswith(".pptx") and magic[:2] != b"PK":
            print("pptx magic check failed")
            return 1
    print("E2E PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
