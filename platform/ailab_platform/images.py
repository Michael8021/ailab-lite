"""Picture search for slides and documents, done by the Agent Service outside the sandbox (which has no network).

Pictures are downloaded here, checked, re-encoded as plain JPEG (no metadata, nothing but pixels reaches the
sandbox) and written into /workspace/assets/images/ through the Broker, with the credit line each one needs in
credits.json next to them (the pptx builder prints it on the slide).

Providers (AILAB_IMAGE_SEARCH_PROVIDER):
  openverse  Openverse (api.openverse.org): openly licensed images, no key needed. Anonymous use is limited to about
             20 searches a minute and 200 a day per server; set AILAB_OPENVERSE_TOKEN for a registered client's limit.
  mock       generated pictures, for tests and offline demos
  none       the search_images tool is not offered
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import logging
import re
import socket
import time
from collections import OrderedDict
from urllib.parse import urlparse

import httpx
from PIL import Image

from .config import settings

log = logging.getLogger("ailab.images")

OPENVERSE = "https://api.openverse.org/v1/images/"
USER_AGENT = "AILab-lite/0.1 (slide image search; https://github.com/shijunan126-creator/ailab-lite)"
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
MAX_EDGE = 1920
# a slide never needs more than this; big originals are downscaled after download (MAX_DOWNLOAD_BYTES caps the transfer)
ASSET_DIR = "/workspace/assets/images"
LICENSE_NAMES = {"cc0": "CC0", "pdm": "公有领域", "by": "CC BY", "by-sa": "CC BY-SA", "by-nd": "CC BY-ND"}


class ImageSearchError(RuntimeError):
    pass


def enabled() -> bool:
    return settings.image_search_provider in ("openverse", "mock")


def _public_host(url: str) -> None:
    """Refuse URLs that point into private networks (a result or a redirect must not reach internal services)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ImageSearchError(f"refusing non-web URL: {url[:100]}")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ImageSearchError(f"cannot resolve {parsed.hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ImageSearchError(f"refusing {parsed.hostname}: resolves to a non-public address")


async def _check_request(request: httpx.Request) -> None:
    _public_host(str(request.url))


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:30]
    return s or "img"


def _reencode(data: bytes) -> tuple[bytes, int, int]:
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            if im.width < 200 or im.height < 150:
                raise ImageSearchError("too small")
            im = im.convert("RGB")
            if max(im.size) > MAX_EDGE:
                im.thumbnail((MAX_EDGE, MAX_EDGE))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=85, optimize=True)
            return out.getvalue(), im.width, im.height
    except ImageSearchError:
        raise
    except Exception as exc:
        raise ImageSearchError(f"not a usable image: {exc}") from exc


def credit_line(hit: dict) -> str:
    lic = LICENSE_NAMES.get(hit.get("license", ""), (hit.get("license") or "").upper())
    if hit.get("license_version") and hit.get("license") not in ("cc0", "pdm"):
        lic = f"{lic} {hit['license_version']}"
    creator = (hit.get("creator") or "").strip()
    source = (hit.get("source") or hit.get("provider") or "").strip()
    parts = [p for p in (f"图片：{creator}" if creator else "图片", source, lic) if p]
    return " / ".join(parts)


class _Cache(OrderedDict):
    """Search results per query for an hour: the anonymous Openverse quota is small."""

    def get_fresh(self, key: str):
        item = self.get(key)
        if item and time.monotonic() - item[0] < 3600:
            self.move_to_end(key)
            return item[1]
        return None

    def put(self, key: str, value) -> None:
        self[key] = (time.monotonic(), value)
        while len(self) > 200:
            self.popitem(last=False)


_cache = _Cache()


async def _openverse(query: str, orientation: str) -> list[dict]:
    key = f"{query}|{orientation}"
    if (hit := _cache.get_fresh(key)) is not None:
        return hit
    # commercial AND modification: slides crop and resize pictures, so no-derivatives (ND) and non-commercial (NC) are out
    params = {"q": query[:200], "license_type": "commercial,modification", "mature": "false", "page_size": "20", "extension": "jpg,png"}
    if orientation in ("wide", "tall", "square"):
        params["aspect_ratio"] = orientation
    headers = {"User-Agent": USER_AGENT}
    if settings.openverse_token:
        headers["Authorization"] = f"Bearer {settings.openverse_token}"
    resp = None
    for attempt in range(2):  # the API is sometimes slow to answer; one retry
        try:
            async with httpx.AsyncClient(timeout=settings.image_search_timeout_seconds, headers=headers) as client:
                resp = await client.get(OPENVERSE, params=params)
            break
        except httpx.TimeoutException as exc:
            if attempt:
                raise ImageSearchError("图片搜索服务响应超时，请稍后再试或不用配图") from exc
    if resp.status_code == 429:
        raise ImageSearchError("图片搜索的频率额度用完了，请稍后再试或不用配图")
    if resp.status_code >= 400:
        raise ImageSearchError(f"image search returned {resp.status_code}")
    results = [r for r in resp.json().get("results", []) if r.get("url") and not r.get("mature")]
    results.sort(key=lambda r: -(min(r.get("width") or 0, 2400) * min(r.get("height") or 0, 1600)))
    _cache.put(key, results)
    return results


async def _download(url: str) -> bytes:
    async with httpx.AsyncClient(
        timeout=settings.image_search_timeout_seconds,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        max_redirects=3,
        event_hooks={"request": [_check_request]},
    ) as client:
        async with client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise ImageSearchError(f"download returned {resp.status_code}")
            chunks, total = [], 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ImageSearchError("image too large")
                chunks.append(chunk)
            return b"".join(chunks)


def _mock_picture(query: str, i: int) -> tuple[bytes, int, int]:
    from PIL import ImageDraw

    w, h = 1600, 1000
    im = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(im)
    seed = sum(map(ord, query)) + i * 37
    for y in range(h):
        draw.line([(0, y), (w, y)], fill=((40 + seed + y // 9) % 256, (90 + y // 12) % 256, (150 + seed // 3) % 256))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=80)
    return out.getvalue(), w, h


async def search_and_fetch(query: str, count: int = 3, orientation: str = "wide") -> list[dict]:
    """Up to `count` pictures for the query: [{"name", "data", "width", "height", "title", "credit"}]."""
    count = max(1, min(int(count or 3), 6))
    if settings.image_search_provider == "mock":
        out = []
        for i in range(count):
            data, w, h = _mock_picture(query, i)
            out.append({"name": f"{slug(query)}_{i + 1}.jpg", "data": data, "width": w, "height": h, "title": query, "credit": ""})
        return out
    if settings.image_search_provider != "openverse":
        raise ImageSearchError("image search is disabled on this platform")
    hits = (await _openverse(query, orientation))[: count * 2 + 2]

    async def fetch(hit: dict) -> dict | None:
        for url in (hit.get("url"), hit.get("thumbnail")):
            if not url:
                continue
            try:
                _public_host(url)
                data, w, h = _reencode(await _download(url))
            except Exception as exc:  # dead links are common in open image indexes: try the next one
                log.info("skipping picture %s: %s", url[:100], exc)
                continue
            return {"data": data, "width": w, "height": h, "title": (hit.get("title") or "")[:120], "credit": credit_line(hit)}
        return None

    # in parallel (a few at a time: image hosts rate-limit bursts); keep the search ranking among the ones that worked
    gate = asyncio.Semaphore(4)

    async def gated(hit: dict) -> dict | None:
        async with gate:
            return await fetch(hit)

    fetched = [f for f in await asyncio.gather(*(gated(h) for h in hits)) if f][:count]
    for i, item in enumerate(fetched, start=1):
        item["name"] = f"{slug(query)}_{i}.jpg"
    return fetched


def merge_credits(existing: bytes | None, new: list[dict]) -> bytes:
    try:
        credits = json.loads(existing.decode("utf-8")) if existing else {}
    except Exception:
        credits = {}
    for item in new:
        credits[item["name"]] = {"attribution": item["credit"], "title": item["title"]}
    return json.dumps(credits, ensure_ascii=False, indent=1).encode("utf-8")
