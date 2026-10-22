"""File processing pipeline: scan, extract text, write derivatives."""

from __future__ import annotations

import io
import json
import logging
from pathlib import PurePath

from ..clients import PlatformClient
from ..storage import Storage

log = logging.getLogger("ailab.worker")

TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".xml", ".html", ".log", ".py", ".sh"}
MAX_TEXT = 200_000


def scan_bytes(data: bytes) -> dict:
    """Placeholder for ClamAV/ICAP; catches the EICAR test string."""
    if b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in data:
        return {"ok": False, "engine": "stub", "reason": "EICAR test signature"}
    return {"ok": True, "engine": "stub"}


def _pdf(data: bytes) -> dict:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = len(reader.pages)
    text = "\n".join((page.extract_text() or "") for page in reader.pages[:20])
    return {"kind": "pdf", "pages": pages, "chars": len(text), "text": text[:MAX_TEXT]}


def _docx(data: bytes) -> dict:
    import docx

    doc = docx.Document(io.BytesIO(data))
    text = "\n".join(p.text for p in doc.paragraphs if p.text)
    for table in doc.tables:
        for row in table.rows:
            text += "\n" + "\t".join(cell.text for cell in row.cells)
    return {"kind": "docx", "paragraphs": len(doc.paragraphs), "chars": len(text), "text": text[:MAX_TEXT]}


def _pptx(data: bytes) -> dict:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    chunks: list[str] = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text.strip():
                chunks.append(shape.text)
    text = "\n".join(chunks)
    return {"kind": "pptx", "slides": len(prs.slides), "chars": len(text), "text": text[:MAX_TEXT]}


def _xlsx(data: bytes) -> dict:
    try:
        import openpyxl
    except ImportError:  # pragma: no cover - optional dependency
        return {"kind": "xlsx", "sheets": 0, "chars": 0, "text": ""}
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    chunks: list[str] = []
    for ws in wb.worksheets:
        chunks.append(f"# {ws.title}")
        for row in ws.iter_rows(max_row=200, values_only=True):
            chunks.append("\t".join("" if v is None else str(v) for v in row))
    text = "\n".join(chunks)
    return {"kind": "xlsx", "sheets": len(wb.worksheets), "chars": len(text), "text": text[:MAX_TEXT]}


def extract(data: bytes, filename: str, content_type: str) -> dict:
    suffix = PurePath(filename).suffix.lower()
    try:
        if suffix == ".pdf" or content_type == "application/pdf":
            return _pdf(data)
        if suffix == ".docx":
            return _docx(data)
        if suffix == ".pptx":
            return _pptx(data)
        if suffix == ".xlsx":
            return _xlsx(data)
    except Exception as exc:
        log.warning("extract failed for %s: %s", filename, exc)
        return {"kind": "error", "error": f"{type(exc).__name__}: {exc}", "chars": 0, "text": ""}

    if suffix in TEXT_SUFFIXES or content_type.startswith("text/"):
        text = data.decode("utf-8", "replace")
        return {"kind": "text", "chars": len(text), "text": text[:MAX_TEXT]}

    return {"kind": "binary", "chars": 0, "text": "", "bytes": len(data)}


def process_file(file_id: str, platform: PlatformClient | None = None, storage: Storage | None = None) -> dict:
    if platform is not None:
        return _process_file(file_id, platform, storage)
    with PlatformClient() as owned:  # holds a connection pool: close it with the file
        return _process_file(file_id, owned, storage)


def _process_file(file_id: str, platform: PlatformClient, storage: Storage | None) -> dict:
    from ..storage import get_storage

    storage = storage or get_storage()
    meta = platform.get_file(file_id)
    if meta.get("status") != "pending":
        return {"file_id": file_id, "skipped": meta.get("status")}
    data = platform.get_file_bytes(file_id)

    scan = scan_bytes(data)
    if not scan["ok"]:
        platform.patch_file(file_id, meta={"processing": "rejected", "scan": scan}, status="rejected")
        return {"file_id": file_id, "rejected": scan}

    info = extract(data, meta["filename"], meta.get("content_type") or "")
    text = info.pop("text", "")
    derived = {
        "file_id": file_id,
        "filename": meta["filename"],
        "content_type": meta.get("content_type"),
        **info,
        "excerpt": text[:1000],
    }
    key = f"derived/{file_id}/meta.json"
    if text:  # the whole extracted text, so a conversation can read an attachment without a sandbox
        derived["text_key"] = f"derived/{file_id}/text.txt"
        storage.put_bytes(derived["text_key"], text.encode("utf-8"), "text/plain; charset=utf-8")
    storage.put_bytes(key, json.dumps(derived, ensure_ascii=False, indent=2).encode(), "application/json")

    platform.patch_file(
        file_id,
        meta={
            "processing": "done",
            "scan": scan,
            "extract": info,
            "derived_key": key,
            **({"text_key": derived["text_key"]} if text else {}),
            "excerpt": text[:300],
        },
        status="ready",
    )
    return {"file_id": file_id, "kind": info.get("kind"), "chars": info.get("chars", 0), "derived_key": key}
