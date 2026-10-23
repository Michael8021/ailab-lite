"""Shared ownership and file deletion helpers."""

from __future__ import annotations

import logging
from pathlib import PurePath

from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .models import Artifact, FileObject, Project, User
from .storage import get_storage

log = logging.getLogger("ailab.library")
THUMBNAIL_VERSION = "webp-480-v1"
RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".ico", ".avif"}


def thumbnail_candidate(filename: str, content_type: str) -> bool:
    """Cheap enqueue hint only; untrusted format detection belongs to the Worker."""
    mime = content_type.split(";", 1)[0].lower()
    suffix = PurePath(filename).suffix.lower()
    return mime != "image/svg+xml" and suffix != ".svg" and (mime.startswith("image/") or suffix in RASTER_SUFFIXES)


def owned_project(db: Session, project_id: str, user: User) -> Project:
    project = db.get(Project, project_id, populate_existing=True)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def delete_file_rows(db: Session, files: list[FileObject], *, detach_children: bool = False) -> list[str]:
    """Delete the ledger in this transaction; return objects to remove after commit."""
    ids = [file.id for file in files]
    if not ids:
        return []
    outside = list(db.scalars(select(FileObject.filename).where(FileObject.parent_id.in_(ids), FileObject.id.not_in(ids))))
    if outside and not detach_children:
        raise HTTPException(status_code=409, detail=f"file has newer versions; delete those first: {', '.join(outside)}")
    keys = []
    for file in files:
        meta = file.meta or {}
        keys.extend(filter(None, (file.storage_key, meta.get("derived_key"), meta.get("text_key"), meta.get("thumb_key"))))
    # Clear links within the selection too, so FK enforcement does not depend on delete order.
    db.execute(update(FileObject).where(FileObject.parent_id.in_(ids)).values(parent_id=None))
    db.execute(delete(Artifact).where(Artifact.file_id.in_(ids)))
    db.execute(delete(FileObject).where(FileObject.id.in_(ids)))
    return keys


def delete_storage_objects(keys: list[str]) -> None:
    storage = get_storage()
    for key in dict.fromkeys(keys):
        try:
            storage.delete(key)
        except Exception:
            log.warning("could not delete object %s", key, exc_info=True)
