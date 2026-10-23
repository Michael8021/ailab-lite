from __future__ import annotations

import io

import pytest
from PIL import Image

from ailab_platform.worker.pipeline import extract, scan_bytes


def image_bytes(fmt="PNG", size=(960, 240), mode="RGB", color="red", **kwargs):
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, fmt, **kwargs)
    return out.getvalue()


class ImagePlatform:
    def __init__(self, data, *, status="pending", filename="image.png", content_type="image/png"):
        self.data = data
        self.file = {"status": status, "filename": filename, "content_type": content_type, "size": len(data), "meta": {}}
        self.reads = 0

    def get_file(self, file_id):
        return self.file

    def get_file_bytes(self, file_id, **kwargs):
        self.reads += 1
        return self.data

    def patch_file(self, file_id, meta=None, status=None):
        self.file["meta"].update(meta or {})
        if status is not None:
            self.file["status"] = status


@pytest.mark.parametrize("fmt,mode", [("PNG", "RGBA"), ("JPEG", "RGB"), ("GIF", "P")])
def test_pipeline_generates_bounded_webp(fmt, mode):
    from ailab_platform.storage import get_storage
    from ailab_platform.worker.pipeline import process_file

    p = ImagePlatform(image_bytes(fmt, mode=mode), content_type="application/octet-stream", filename="unknown.bin")
    process_file(f"fil_thumb_{fmt}", platform=p)
    assert p.file["status"] == "ready"
    key = p.file["meta"]["thumb_key"]
    assert key == f"derived/fil_thumb_{fmt}/thumb.webp"
    with Image.open(io.BytesIO(get_storage().get_bytes(key))) as thumb:
        assert thumb.format == "WEBP" and thumb.size == (480, 120)


@pytest.mark.parametrize("fmt,expected", [("JPEG", True), ("PNG", False)])
def test_pixel_cap_spares_jpeg_photos_but_bounds_full_decodes(fmt, expected, monkeypatch):
    from ailab_platform.worker import pipeline

    # Scaled-down stand-in for a 24 MP photo vs a 24 MP PNG: JPEG draft-decodes small, PNG would not.
    monkeypatch.setattr(pipeline, "MAX_DECODED_PIXELS", 100_000)
    p = ImagePlatform(image_bytes(fmt, size=(600, 400)), content_type=f"image/{fmt.lower()}")
    pipeline.process_file(f"fil_cap_{fmt}", platform=p)
    assert p.file["status"] == "ready" and ("thumb_key" in p.file["meta"]) is expected


def test_thumbnail_honours_exif_orientation():
    from ailab_platform.storage import get_storage
    from ailab_platform.worker.pipeline import process_file

    exif = Image.Exif()
    exif[274] = 6
    p = ImagePlatform(image_bytes("JPEG", exif=exif))
    process_file("fil_exif", platform=p)
    with Image.open(io.BytesIO(get_storage().get_bytes(p.file["meta"]["thumb_key"]))) as thumb:
        assert thumb.size == (120, 480)


@pytest.mark.parametrize("kind", ["corrupt", "bomb_warning", "bomb_error", "svg", "oversize", "storage_error"])
@pytest.mark.parametrize("thumbnail_only", [False, True])
def test_bad_thumbnail_never_rejects_file(kind, thumbnail_only, monkeypatch):
    from ailab_platform.storage import get_storage
    from ailab_platform.worker import pipeline

    data = image_bytes(size=(20, 20))
    content_type = "image/png"
    if kind == "corrupt":
        data = b"not an image"
    elif kind.startswith("bomb"):
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 300 if kind == "bomb_warning" else 100)
    elif kind == "svg":
        content_type = "image/svg+xml"
    elif kind == "oversize":
        monkeypatch.setattr(pipeline, "MAX_THUMBNAIL_BYTES", 1)
    elif kind == "storage_error":
        storage = get_storage()
        put = storage.put_bytes

        def fail_thumb(key, *args):
            if key.endswith("thumb.webp"):
                raise OSError("offline")
            return put(key, *args)

        monkeypatch.setattr(storage, "put_bytes", fail_thumb)
    p = ImagePlatform(data, content_type=content_type, status="ready" if thumbnail_only else "pending")
    pipeline.process_file(f"fil_bad_{kind}_{thumbnail_only}", platform=p, thumbnail_only=thumbnail_only)
    assert p.file["status"] == "ready" and "thumb_key" not in p.file["meta"]


def test_thumbnail_waits_for_scan_and_oversize_ready_file_is_not_downloaded():
    from ailab_platform.worker.pipeline import MAX_THUMBNAIL_BYTES, process_file

    p = ImagePlatform(image_bytes() + b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE")
    process_file("fil_scan_first", platform=p)
    assert p.file["status"] == "rejected" and "thumb_key" not in p.file["meta"]
    p = ImagePlatform(b"", status="ready")
    p.file["size"] = MAX_THUMBNAIL_BYTES + 1
    process_file("fil_large_ready", platform=p, thumbnail_only=True)
    assert p.reads == 0 and p.file["status"] == "ready"


def test_thumbnail_only_is_idempotent_and_preserves_ready_metadata():
    from ailab_platform.worker.pipeline import process_file

    p = ImagePlatform(image_bytes(), status="ready")
    p.file["meta"] = {"source": "sandbox"}
    process_file("fil_art_thumb", platform=p, thumbnail_only=True)
    assert p.file["status"] == "ready" and p.file["meta"]["source"] == "sandbox"
    assert "thumb_key" in p.file["meta"] and "scan" not in p.file["meta"]
    process_file("fil_art_thumb", platform=p, thumbnail_only=True)
    assert p.reads == 1
    for status in ("pending", "rejected"):
        p = ImagePlatform(image_bytes(), status=status)
        process_file("fil_art_thumb", platform=p, thumbnail_only=True)
        assert p.reads == 0 and p.file["status"] == status


def test_thumbnail_only_queue_failures_never_reject_ready(monkeypatch):
    from ailab_platform.config import settings
    from ailab_platform.queue import InMemoryQueue
    from ailab_platform.worker import main

    calls = []

    def fail(fid, **kw):
        calls.append(kw)
        raise RuntimeError("API unavailable")

    monkeypatch.setattr(main, "process_file", fail)
    monkeypatch.setattr(main, "_patch", lambda *args, **kw: pytest.fail("must not alter READY status or processing"))
    q = InMemoryQueue()
    q.enqueue(settings.file_queue, {"file_id": "f", "thumbnail_only": True})
    msg = q.dequeue(settings.file_queue, "w", timeout=1)
    main.handle_message(q, msg)
    assert calls == [{"thumbnail_only": True}]
    assert q.outstanding(settings.file_queue)
    msg.deliveries = settings.file_max_attempts + 1
    main.handle_message(q, msg)
    assert not q.outstanding(settings.file_queue)


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

        def close(self):
            pass

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


def test_the_full_text_is_kept_for_the_agent() -> None:
    from ailab_platform.storage import get_storage
    from ailab_platform.worker.pipeline import process_file

    class Platform:
        def __init__(self):
            self.meta = None

        def get_file(self, file_id):
            return {"status": "pending", "filename": "a.txt", "content_type": "text/plain"}

        def get_file_bytes(self, file_id):
            return ("长文本" * 1000).encode()

        def patch_file(self, file_id, meta=None, status=None):
            self.meta = meta

    p = Platform()
    process_file("fil_textkeep", platform=p, storage=get_storage())
    assert p.meta["text_key"] == "derived/fil_textkeep/text.txt"
    assert get_storage().get_bytes(p.meta["text_key"]).decode() == "长文本" * 1000


def test_large_file_is_not_parsed(monkeypatch) -> None:
    """The Worker keeps the file usable but skips a parser that could OOM it."""
    from ailab_platform.config import settings
    from ailab_platform.storage import get_storage
    from ailab_platform.worker.pipeline import process_file

    monkeypatch.setattr(settings, "max_parse_bytes", 5)

    class Platform:
        def __init__(self):
            self.meta = None
            self.status = None

        def get_file(self, file_id):
            return {"status": "pending", "filename": "big.pdf", "content_type": "application/pdf"}

        def get_file_bytes(self, file_id):
            return b"%PDF-1.7" + b"x" * 100

        def patch_file(self, file_id, meta=None, status=None):
            self.meta = meta
            self.status = status

    platform = Platform()
    process_file("fil_bigparse", platform=platform, storage=get_storage())
    assert platform.meta["extract"]["kind"] == "too_large"
    assert platform.meta["extract"]["truncated"] is True
    assert platform.status == "ready"


def test_bounded_thumbnail_source_read_closes_http_response():
    import httpx

    from ailab_platform.clients import PlatformClient

    closed = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x" * 64 * 1024
            yield b"y" * 64 * 1024
            pytest.fail("read beyond cap")

        def close(self):
            closed.append(True)

    with PlatformClient() as client:
        client._http.close()
        client._http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=Stream())))
        with pytest.raises(ValueError, match="bounded read"):
            client.get_file_bytes("f", max_bytes=64 * 1024)
        assert closed == [True]


def test_thumbnail_uses_first_animation_frame_and_keeps_alpha():
    from ailab_platform.storage import get_storage
    from ailab_platform.worker.pipeline import process_file

    output = io.BytesIO()
    Image.new("RGB", (100, 50), "red").save(
        output, "GIF", save_all=True, append_images=[Image.new("RGB", (100, 50), "blue")], duration=100, loop=0
    )
    p = ImagePlatform(output.getvalue())
    process_file("fil_animation", platform=p)
    with Image.open(io.BytesIO(get_storage().get_bytes(p.file["meta"]["thumb_key"]))) as thumb:
        assert thumb.size == (100, 50) and getattr(thumb, "n_frames", 1) == 1
        red, _, blue = thumb.convert("RGB").getpixel((50, 25))
        assert red > 200 and blue < 20
    p = ImagePlatform(image_bytes(mode="RGBA", color=(255, 0, 0, 0)))
    process_file("fil_alpha", platform=p)
    with Image.open(io.BytesIO(get_storage().get_bytes(p.file["meta"]["thumb_key"]))) as thumb:
        assert thumb.mode == "RGBA" and thumb.getextrema()[3] == (0, 0)
