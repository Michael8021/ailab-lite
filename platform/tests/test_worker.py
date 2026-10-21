from __future__ import annotations

from ailab_platform.worker.pipeline import extract, scan_bytes


def test_text_extract() -> None:
    info = extract(b"hello\nworld", "a.txt", "text/plain")
    assert info["kind"] == "text"
    assert "world" in info["text"]


def test_eicar_rejected() -> None:
    assert scan_bytes(b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE")["ok"] is False
    assert scan_bytes(b"normal bytes")["ok"] is True


def test_binary_fallback() -> None:
    info = extract(b"\x00\x01", "a.bin", "application/octet-stream")
    assert info["kind"] == "binary"


def test_worker_message_handling(monkeypatch) -> None:
    from ailab_platform.config import settings
    from ailab_platform.queue import InMemoryQueue
    from ailab_platform.worker import main as worker_main

    patched: list[tuple] = []

    class FakePlatform:
        def patch_file(self, file_id, meta=None, status=None):
            patched.append((file_id, meta, status))

    monkeypatch.setattr(worker_main, "PlatformClient", FakePlatform)
    q = InMemoryQueue()

    # success -> acked
    monkeypatch.setattr(worker_main, "process_file", lambda fid: {"ok": fid})
    q.enqueue(settings.file_queue, {"file_id": "f1"})
    worker_main.handle_message(q, q.dequeue(settings.file_queue, "w", timeout=1))
    assert q.outstanding(settings.file_queue) == []

    # failure -> flagged, NOT acked (redelivered later), file stays quarantined
    def boom(fid):
        raise RuntimeError("parser crashed")

    monkeypatch.setattr(worker_main, "process_file", boom)
    q.enqueue(settings.file_queue, {"file_id": "f2"})
    worker_main.handle_message(q, q.dequeue(settings.file_queue, "w", timeout=1))
    assert q.outstanding(settings.file_queue) == [{"file_id": "f2"}]
    assert patched[-1] == ("f2", {"processing": "error"}, None)

    # poison -> rejected (fail closed, and then deletable), acked, never marked ready
    msg = q.dequeue(settings.file_queue, "w", timeout=1, visibility_seconds=0)
    msg.deliveries = settings.file_max_attempts + 1
    worker_main.handle_message(q, msg)
    assert q.outstanding(settings.file_queue) == []
    assert patched[-1][0] == "f2" and patched[-1][2] == "rejected"
    assert all(status != "ready" for _, _, status in patched)
