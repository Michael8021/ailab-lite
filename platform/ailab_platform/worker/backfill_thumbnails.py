"""Enqueue missing library thumbnails using bounded, read-only keyset batches."""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import func, or_, select

from ..config import settings
from ..db import SessionLocal
from ..library import RASTER_SUFFIXES, thumbnail_candidate
from ..models import FileObject, FileStatus
from ..queue import get_queue

log = logging.getLogger("ailab.worker.backfill")


def backfill_thumbnails(*, batch_size: int = 200) -> dict[str, int]:
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    queue = get_queue()
    cursor = ""
    enqueued = failed = 0
    while True:
        # Release the connection between batches and before queue I/O. Never load original bytes here.
        with SessionLocal() as db:
            rows = db.execute(
                select(FileObject.id, FileObject.filename, FileObject.content_type)
                .where(
                    FileObject.id > cursor,
                    FileObject.status == FileStatus.READY,
                    or_(FileObject.meta["thumb_key"].as_string().is_(None), FileObject.meta["thumb_key"].as_string() == ""),
                    or_(
                        FileObject.content_type.like("image/%"),
                        *(func.lower(FileObject.filename).like(f"%{suffix}") for suffix in sorted(RASTER_SUFFIXES)),
                    ),
                )
                .order_by(FileObject.id)
                .limit(batch_size)
            ).all()
        if not rows:
            break
        cursor = rows[-1].id
        for file_id, filename, content_type in rows:
            if not thumbnail_candidate(filename, content_type):
                continue
            try:
                queue.enqueue(settings.file_queue, {"file_id": file_id, "thumbnail_only": True})
                enqueued += 1
            except Exception:
                failed += 1
                log.warning("could not enqueue thumbnail for %s", file_id, exc_info=True)
    return {"enqueued": enqueued, "failed": failed}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=200, choices=range(1, 1001), metavar="1..1000")
    args = parser.parse_args()
    if settings.queue_backend == "memory":
        parser.error("backfill requires a shared queue: configure AILAB_QUEUE_BACKEND=redis")
    logging.basicConfig(level=logging.INFO)
    result = backfill_thumbnails(batch_size=args.batch_size)
    print(result)
    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
