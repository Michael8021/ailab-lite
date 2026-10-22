"""File library: upload, list, download and presigned-upload compatibility."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..deps import current_user, get_db
from ..locks import user_critical_section
from ..models import Artifact, FileObject, FileStatus, User, new_id
from ..queue import get_queue
from ..quota import enforce_library_quota
from ..schemas import FileOut
from ..storage import get_storage
from .serializers import file_dict

router = APIRouter(prefix="/v1/files", tags=["files"])
log = logging.getLogger("ailab.api.files")


class PresignIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = "application/octet-stream"


def safe_filename(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name.replace("\x00", "").strip()
    return (name or "file")[:200]


def content_disposition(filename: str) -> str:
    ascii_name = "".join(c for c in filename if 32 <= ord(c) < 127) or "download"
    ascii_name = ascii_name.replace("\\", "\\\\").replace('"', '\\"')
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _owned_file(db: Session, file_id: str, user: User) -> FileObject:
    file = db.get(FileObject, file_id)
    if file is None or file.user_id != user.id:
        raise HTTPException(status_code=404, detail="file not found")
    return file


@router.post("", response_model=FileOut, status_code=status.HTTP_201_CREATED)
async def upload_file(
    upload: UploadFile = File(...),
    parent_id: str | None = Query(default=None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    # Stream Starlette's spooled upload to a temp file while hashing. Only the
    # chunk and the object-storage copy exist at once, never the whole file twice.
    tmp = tempfile.NamedTemporaryFile(prefix="ailab-upload-", delete=False)
    storage = get_storage()
    try:
        digest = hashlib.sha256()
        size = 0
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > settings.max_upload_bytes:
                raise HTTPException(status_code=413, detail=f"file exceeds {settings.max_upload_bytes} bytes")
            digest.update(chunk)
            tmp.write(chunk)
        tmp.close()
        if size == 0:
            raise HTTPException(status_code=400, detail="empty file")

        filename = safe_filename(upload.filename or "file")
        content_type = upload.content_type or "application/octet-stream"
        file_id = new_id("fil")
        key = f"uploads/{user.id}/{file_id}/{filename}"
        try:
            await asyncio.to_thread(storage.put_file, key, tmp.name, content_type)
            with user_critical_section(db, user.id):  # quota check and the file row are one step
                enforce_library_quota(db, user.id, size)
                version = 1
                if parent_id:
                    parent = _owned_file(db, parent_id, user)
                    version = parent.version + 1
                file = FileObject(
                    id=file_id,
                    user_id=user.id,
                    filename=filename,
                    content_type=content_type,
                    size=size,
                    sha256=digest.hexdigest(),
                    storage_key=key,
                    status=FileStatus.PENDING,
                    version=version,
                    parent_id=parent_id,
                    meta={"processing": "queued"},
                )
                db.add(file)
                db.commit()
                db.refresh(file)
        except Exception:
            storage.delete(key)
            raise
    finally:
        tmp.close()
        os.unlink(tmp.name)

    try:
        get_queue().enqueue(settings.file_queue, {"file_id": file.id})
    except Exception:
        log.warning("file worker queue unavailable for %s", file.id, exc_info=True)
    return file_dict(file)


@router.get("", response_model=list[FileOut])
def list_files(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    files = db.scalars(
        select(FileObject).where(FileObject.user_id == user.id).order_by(FileObject.created_at.desc()).limit(limit).offset(offset)
    ).all()
    return [file_dict(f) for f in files]


@router.get("/{file_id}", response_model=FileOut)
def get_file(file_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return file_dict(_owned_file(db, file_id, user))


@router.get("/{file_id}/content")
def download_file(file_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    file = _owned_file(db, file_id, user)
    if file.status != FileStatus.READY:
        raise HTTPException(status_code=409, detail=f"file is {file.status}, not downloadable")
    # Do not pin the request's DB connection for the duration of the download:
    # the stream reads object storage only. Closing here is safe (the dependency
    # close is idempotent).
    db.close()
    return StreamingResponse(
        get_storage().stream(file.storage_key),
        media_type=file.content_type,
        headers={
            "Content-Disposition": content_disposition(file.filename),
            "Content-Length": str(file.size),
        },
    )


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(file_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    """Remove a file (including rejected / unscannable ones) and free its library space."""
    file = _owned_file(db, file_id, user)
    newer = db.scalar(select(func.count()).select_from(FileObject).where(FileObject.parent_id == file.id))
    if newer:
        raise HTTPException(status_code=409, detail="file has newer versions; delete those first")
    db.execute(delete(Artifact).where(Artifact.file_id == file.id))
    keys = [file.storage_key, (file.meta or {}).get("derived_key"), (file.meta or {}).get("text_key")]
    db.delete(file)
    db.commit()
    storage = get_storage()
    for key in filter(None, keys):
        try:
            storage.delete(key)
        except Exception:
            log.warning("could not delete object %s", key, exc_info=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/presign", status_code=200)
def presign_upload(body: PresignIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """S3/SeaweedFS direct upload. Local storage returns 501 on purpose."""
    file_id = new_id("fil")
    filename = safe_filename(body.filename)
    key = f"uploads/{user.id}/{file_id}/{filename}"
    url = get_storage().presign_put(key, expires=900)
    if url is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="storage backend does not support presigned upload; use POST /v1/files",
        )
    file = FileObject(
        id=file_id,
        user_id=user.id,
        filename=filename,
        content_type=body.content_type,
        storage_key=key,
        status=FileStatus.PENDING,
        meta={"processing": "awaiting_upload"},
    )
    db.add(file)
    db.commit()
    return {"file_id": file_id, "upload_url": url, "method": "PUT", "expires_in": 900}


@router.post("/{file_id}/complete", response_model=FileOut)
def complete_upload(file_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    storage = get_storage()
    with user_critical_section(db, user.id):
        file = _owned_file(db, file_id, user)
        db.refresh(file)  # a simultaneous completion may have finished while we waited for the lock
        if (file.meta or {}).get("processing") != "awaiting_upload":
            raise HTTPException(status_code=409, detail=f"file already {file.status}")
        signed_key = file.storage_key
        if not storage.exists(signed_key):
            raise HTTPException(status_code=409, detail="object not uploaded yet")
        # A signed PUT is reusable until it expires. Freeze a separate object BEFORE hashing/scanning:
        # replacing the signed object later must never replace the bytes the Worker approved.
        final_key = f"uploads/{user.id}/{file.id}/{new_id('obj')}/{file.filename}"
        try:
            storage.copy(signed_key, final_key)
            size = storage.size(final_key)
            if size == 0:
                raise HTTPException(status_code=400, detail="empty file")
            if size > settings.max_upload_bytes:
                raise HTTPException(status_code=413, detail=f"file exceeds {settings.max_upload_bytes} bytes")
            enforce_library_quota(db, user.id, size)
            digest = hashlib.sha256()
            for chunk in storage.stream(final_key):
                digest.update(chunk)
            file.storage_key = final_key
            file.size = size
            file.sha256 = digest.hexdigest()
            file.meta = {"processing": "queued"}
            db.commit()
        except Exception as exc:
            db.rollback()
            storage.delete(final_key)
            if isinstance(exc, HTTPException) and exc.status_code in (400, 413):
                # Invalid/over-quota uploads are terminal rejections, as with multipart uploads. Do not
                # retain an unaccounted object simply because no FileObject.size was committed for it.
                storage.delete(signed_key)
                db.delete(file)
                db.commit()
            raise
        # The DB now references only the frozen copy. Cleanup failure must not undo an accepted upload.
        try:
            storage.delete(signed_key)
        except Exception:
            log.warning("could not delete completed upload object %s", signed_key, exc_info=True)
    try:
        get_queue().enqueue(settings.file_queue, {"file_id": file.id})
    except Exception:
        log.warning("file worker queue unavailable for %s", file.id, exc_info=True)
    return file_dict(file)
