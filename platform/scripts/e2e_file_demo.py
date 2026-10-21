"""Upload a file, wait for the Worker, then run a task with it as input."""

from __future__ import annotations

import os
import sys
import time

import httpx

BASE = os.environ.get("AILAB_API_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
TOKEN = os.environ.get("AILAB_DEV_TOKEN", "dev-demo-token")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
SERVICE = {"X-Service-Token": os.environ.get("AILAB_SERVICE_TOKEN", "dev-service-token")}


def main() -> int:
    with httpx.Client(timeout=60) as client:
        upload = client.post(
            f"{BASE}/v1/files",
            headers=HEADERS,
            files={"upload": ("note.txt", "AILab 平台测试资料：沙箱隔离、模型网关、对象存储。\n".encode(), "text/plain")},
        )
        if upload.status_code >= 400:
            print("upload failed", upload.status_code, upload.text)
            return 1
        file_id = upload.json()["id"]
        print("file", file_id)

        for _ in range(40):
            meta = client.get(f"{BASE}/v1/files/{file_id}", headers=HEADERS).json()
            if meta.get("meta", {}).get("processing") == "done":
                print("worker done:", meta["meta"].get("extract"))
                break
            time.sleep(0.5)
        else:
            print("worker did not finish:", meta.get("meta"))
            return 1

        run = client.post(
            f"{BASE}/v1/runs",
            headers=HEADERS,
            json={"prompt": "总结这份资料并生成一个 markdown 文件", "file_ids": [file_id]},
        )
        if run.status_code >= 400:
            print("create run failed", run.status_code, run.text)
            return 1
        run_id = run.json()["id"]
        print("run", run_id)

        detail = {}
        for _ in range(300):
            detail = client.get(f"{BASE}/v1/runs/{run_id}", headers=HEADERS).json()
            if detail["status"] in {"succeeded", "failed", "canceled"}:
                break
            time.sleep(0.5)
        print("status", detail.get("status"), "error", detail.get("error"))
        if detail.get("status") != "succeeded":
            return 1
        artifacts = detail.get("artifacts") or []
        if not artifacts or not any(a["filename"].endswith(".md") for a in artifacts):
            print("missing markdown artifact", artifacts)
            return 1
        content = client.get(f"{BASE}{artifacts[0]['download_url']}", headers=HEADERS).content
        print("artifact", artifacts[0]["filename"], len(content), "bytes")
        print("E2E FILE PASS")
        return 0


if __name__ == "__main__":
    sys.exit(main())
