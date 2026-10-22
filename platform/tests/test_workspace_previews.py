"""Broker: a conversation's workspace survives between turns; office files in outputs get previews."""

from __future__ import annotations

import io
import json
import shutil

from openpyxl import Workbook

from ailab_platform.broker.service import LeaseManager
from ailab_platform.sandbox.mock import MockSandbox
from ailab_platform.storage import LocalStorage

from .fakes import FakeRedis


class _Api:
    def __init__(self) -> None:
        self.runs: dict[str, dict] = {}

    def get_run(self, run_id):
        return {"id": run_id, "input_file_ids": [], **self.runs.get(run_id, {})}

    def append_event(self, *_a, **_k):
        return {}


def _manager(tmp_path):
    api = _Api()
    storage = LocalStorage(tmp_path / "store")
    return LeaseManager(provider=MockSandbox(), platform=api, storage=storage, redis_client=FakeRedis()), api, storage


def _xlsx(value: int) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["区域", "金额"])
    ws.append(["华东", value])
    ws.append(["合计", "=SUM(B2:B2)"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_the_workspace_comes_back_in_the_next_turn_without_inputs_or_platform_files(tmp_path) -> None:
    manager, api, storage = _manager(tmp_path)
    first = manager.create("run_1")
    manager.put_file(first["lease_id"], "/workspace/work/deck.json", b'{"slides": []}')
    manager.put_file(first["lease_id"], "/workspace/outputs/deck.pptx", b"PK fake")
    manager.put_file(first["lease_id"], "/workspace/inputs/upload.txt", b"user upload")
    saved = manager.snapshot(first["lease_id"])
    assert saved["key"] == "workspaces/run_1.tgz" and storage.exists(saved["key"])
    manager.release(first["lease_id"])

    api.runs["run_2"] = {"workspace_restore_key": "workspaces/run_1.tgz"}
    second = manager.create("run_2")
    assert second["restored"] is True
    files = {f["path"] for f in manager.list_files(second["lease_id"], "/workspace")}
    assert {"/workspace/work/deck.json", "/workspace/outputs/deck.pptx"} <= files
    assert "/workspace/inputs/upload.txt" not in files  # inputs belong to the turn that uploaded them
    assert not any("/.ailab/" in f for f in files)
    assert manager.get_file(second["lease_id"], "/workspace/work/deck.json") == b'{"slides": []}'


def test_a_broken_snapshot_costs_continuity_not_the_turn(tmp_path) -> None:
    manager, api, storage = _manager(tmp_path)
    storage.put_bytes("workspaces/run_0.tgz", b"not a tarball")
    api.runs["run_1"] = {"workspace_restore_key": "workspaces/run_0.tgz"}
    lease = manager.create("run_1")
    assert lease["restored"] is False and lease["restore_error"]
    assert manager.exec(lease["lease_id"], "echo ok").stdout.strip() == "ok"


def test_previews_are_rendered_once_per_change(tmp_path) -> None:
    manager, _, storage = _manager(tmp_path)
    lease = manager.create("run_p")["lease_id"]
    assert manager.refresh_previews(lease) == []

    manager.put_file(lease, "/workspace/outputs/sales.xlsx", _xlsx(100))
    [first] = manager.refresh_previews(lease)
    assert first["filename"] == "sales.xlsx" and first["kind"] == "sheets" and first["version"] == 1
    data = json.loads(storage.get_bytes(f"previews/run_p/{first['data']}"))
    cells = [[c["v"] for c in row] for row in data["sheets"][0]["rows"]]
    assert cells[1] == ["华东", "100"]
    assert manager.refresh_previews(lease) == []  # unchanged: nothing to do

    manager.put_file(lease, "/workspace/outputs/sales.xlsx", _xlsx(250) + b"")
    manager.exec(lease, "touch -d '+1 minute' /workspace/outputs/sales.xlsx")
    [second] = manager.refresh_previews(lease)
    assert second["version"] == 2
    assert not storage.exists("workspaces/run_p.tgz")


def test_a_preview_that_cannot_be_rendered_is_reported_not_retried(tmp_path, monkeypatch) -> None:
    # Keep the GNU find selected by the test environment (on macOS it is outside /usr/bin), while excluding LibreOffice.
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "find").symlink_to(shutil.which("find"))
    monkeypatch.setenv("PATH", f"{tools}:/usr/bin:/bin")
    manager, _, _ = _manager(tmp_path)
    lease = manager.create("run_e")["lease_id"]
    manager.put_file(lease, "/workspace/outputs/deck.pptx", b"not really a deck")
    [failed] = manager.refresh_previews(lease)
    assert failed["filename"] == "deck.pptx" and failed["error"]
    assert manager.refresh_previews(lease) == []
