"""Picture search: what reaches the sandbox is a re-encoded JPEG from a public host, with its credit line."""

from __future__ import annotations

import asyncio
import io
import json

import httpx
import pytest
from PIL import Image

from ailab_platform import images
from ailab_platform.config import settings


def _png(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), (10, 120, 200, 255)).save(buf, "PNG")
    return buf.getvalue()


def test_pictures_are_reencoded_as_plain_jpeg_and_downscaled() -> None:
    data, w, h = images._reencode(_png(3000, 2000))
    assert data[:2] == b"\xff\xd8" and (w, h) == (1920, 1280)
    with pytest.raises(images.ImageSearchError, match="too small"):
        images._reencode(_png(100, 80))
    with pytest.raises(images.ImageSearchError, match="not a usable image"):
        images._reencode(b"<html>not an image</html>")


@pytest.mark.parametrize("url", ["http://127.0.0.1/x.jpg", "http://10.0.0.5/x.jpg", "http://169.254.169.254/latest", "file:///etc/passwd"])
def test_private_and_non_web_addresses_are_refused(url) -> None:
    with pytest.raises(images.ImageSearchError):
        images._public_host(url)


def test_credit_lines_and_the_credits_file() -> None:
    hit = {"creator": "Ann", "source": "flickr", "license": "by", "license_version": "2.0"}
    assert images.credit_line(hit) == "图片：Ann / flickr / CC BY 2.0"
    assert images.credit_line({"license": "cc0"}) == "图片 / CC0"
    merged = json.loads(
        images.merge_credits(b'{"a.jpg": {"attribution": "x", "title": ""}}', [{"name": "b.jpg", "credit": "y", "title": "t"}])
    )
    assert set(merged) == {"a.jpg", "b.jpg"}
    assert json.loads(images.merge_credits(b"garbage", [])) == {}


def test_openverse_results_are_fetched_in_rank_order_skipping_dead_links(monkeypatch) -> None:
    monkeypatch.setattr(settings, "image_search_provider", "openverse")
    hits = [{"url": f"https://img.example/{i}.jpg", "title": f"t{i}", "creator": "c", "license": "by", "source": "s"} for i in range(5)]

    async def fake_search(query, orientation):
        return hits

    async def fake_download(url):
        if url.endswith("/1.jpg"):
            raise images.ImageSearchError("download returned 404")
        if url.endswith("/0.jpg"):
            await asyncio.sleep(0.05)  # slower, but ranked first: still comes first
        return _png(800, 600)

    monkeypatch.setattr(images, "_openverse", fake_search)
    monkeypatch.setattr(images, "_download", fake_download)
    monkeypatch.setattr(images, "_public_host", lambda url: None)
    got = asyncio.run(images.search_and_fetch("old town", 3))
    assert [g["title"] for g in got] == ["t0", "t2", "t3"]
    assert [g["name"] for g in got] == ["old_town_1.jpg", "old_town_2.jpg", "old_town_3.jpg"]


def test_a_rate_limited_search_says_so(monkeypatch) -> None:
    monkeypatch.setattr(settings, "image_search_provider", "openverse")
    images._cache.clear()

    class Limited(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(429)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=Limited(), **kw))
    with pytest.raises(images.ImageSearchError, match="额度"):
        asyncio.run(images.search_and_fetch("anything", 2))


def test_disabled_search_is_not_offered(monkeypatch) -> None:
    from ailab_platform.agent.tools import tools_for_turn

    monkeypatch.setattr(settings, "image_search_provider", "none")
    assert "search_images" not in [t["function"]["name"] for t in tools_for_turn()]
    monkeypatch.setattr(settings, "image_search_provider", "mock")
    assert "search_images" in [t["function"]["name"] for t in tools_for_turn()]
