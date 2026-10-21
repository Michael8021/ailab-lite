"""Internal service-to-service API.

Every route requires ``X-Service-Token``. Agent, Broker and Worker use these
endpoints instead of touching the database or object storage ledger directly.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import PurePosixPath
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..deps import get_db, service_auth
from ..events import append_event
from ..models import Artifact, FileObject, FileStatus, ModelUsage, Run, RunStatus, new_id
from ..run_state import InvalidTransition, transition_run
from ..schemas import (
    ArtifactIn,
    CommitOut,
    EventIn,
    EventOut,
    FileOut,
    FilePatch,
    RunDetail,
    RunStatusIn,
    UsageIn,
)
from ..storage import get_storage
from .serializers import file_dict, run_dict

router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(service_auth)])
log = logging.getLogger("ailab.api.internal")


def _run_or_404(db: Session, run_id: str) -> Run:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.get("/runs/{run_id}", response_model=RunDetail)
def internal_get_run(run_id: str, db: Session = Depends(get_db)) -> dict:
    return run_dict(db, _run_or_404(db, run_id), detail=True)


@router.post("/runs/{run_id}/status", response_model=RunDetail)
def internal_set_status(run_id: str, body: RunStatusIn, db: Session = Depends(get_db)) -> dict:
    _run_or_404(db, run_id)
    if body.status not in {RunStatus.RUNNING, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED}:
        raise HTTPException(status_code=400, detail=f"bad status: {body.status}")
    try:
        run, _ = transition_run(db, run_id, body.status, summary=body.summary, error=body.error, result=body.result)
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=f"invalid transition: {exc}") from exc
    return run_dict(db, run, detail=True)


@router.post("/runs/{run_id}/events", response_model=EventOut)
def internal_append_event(run_id: str, body: EventIn, db: Session = Depends(get_db)) -> dict:
    _run_or_404(db, run_id)
    event = append_event(db, run_id, body.type, body.payload)
    return {
        "seq": event.seq,
        "type": event.type,
        "payload": event.payload,
        "created_at": event.created_at,
    }


@router.post("/runs/{run_id}/usage")
def internal_usage(run_id: str, body: UsageIn, db: Session = Depends(get_db)) -> dict:
    _run_or_404(db, run_id)
    db.add(
        ModelUsage(
            run_id=run_id,
            model=body.model,
            prompt_tokens=body.prompt_tokens,
            completion_tokens=body.completion_tokens,
        )
    )
    db.commit()
    return {"ok": True}


def _artifact_out(artifact: Artifact, file: FileObject) -> dict:
    return {
        "artifact_id": artifact.id,
        "file_id": file.id,
        "filename": file.filename,
        "content_type": file.content_type,
        "size": file.size,
        "sha256": file.sha256,
        "download_url": f"/v1/files/{file.id}/content",
    }


def _artifact_filename(name: str) -> str:
    name = PurePosixPath(name.replace("\\", "/")).name.strip()
    return name[:200] if name not in ("", ".", "..") else "artifact"


@router.post("/artifacts", response_model=CommitOut)
def internal_commit_artifact(body: ArtifactIn, db: Session = Depends(get_db)) -> dict:
    """Second half of the two-phase delivery. Safe to retry.

    Same (run, filename, sha256) returns the artifact that already exists instead
    of creating a duplicate, and the staging object is only removed after the
    database row is committed, so a failure in between can be retried.
    """
    run = _run_or_404(db, body.run_id)
    if not body.staging_key.startswith("staging/"):
        raise HTTPException(status_code=400, detail="staging_key must be under staging/")
    storage = get_storage()

    def reject(code: int, detail: str) -> HTTPException:
        # Nobody will ever look at this staging object again; don't leak it.
        storage.delete(body.staging_key)
        return HTTPException(status_code=code, detail=detail)

    if run.status != RunStatus.RUNNING:
        raise reject(409, f"run is {run.status}; artifacts are only accepted while it runs")
    if body.size > settings.max_artifact_bytes:
        raise reject(413, f"artifact exceeds {settings.max_artifact_bytes} bytes")
    filename = _artifact_filename(body.filename)

    existing = db.execute(
        select(Artifact, FileObject)
        .join(FileObject, Artifact.file_id == FileObject.id)
        .where(Artifact.run_id == run.id, FileObject.filename == filename, FileObject.sha256 == body.sha256)
    ).first()
    if existing is not None:
        storage.delete(body.staging_key)
        return _artifact_out(*existing)

    if not storage.exists(body.staging_key):
        raise HTTPException(status_code=409, detail="staging object not found")
    data = storage.get_bytes(body.staging_key)
    if len(data) != body.size:
        raise reject(400, f"size mismatch: got {len(data)} expected {body.size}")
    digest = hashlib.sha256(data).hexdigest()
    if digest != body.sha256:
        raise reject(400, "sha256 mismatch")

    file_id = new_id("fil")
    final_key = f"runs/{run.id}/{file_id}/{filename}"
    storage.copy(body.staging_key, final_key)

    file = FileObject(
        id=file_id,
        user_id=run.user_id,
        filename=filename,
        content_type=body.content_type,
        size=len(data),
        sha256=digest,
        storage_key=final_key,
        status=FileStatus.READY,
        meta={"source": "sandbox", "run_id": run.id},
    )
    artifact = Artifact(run_id=run.id, file_id=file_id, role=body.role)
    try:
        db.add(file)
        db.add(artifact)
        db.commit()
    except Exception:
        db.rollback()
        storage.delete(final_key)  # nothing references it; the staging copy is still there for the retry
        raise
    db.refresh(file)
    db.refresh(artifact)
    storage.delete(body.staging_key)

    append_event(
        db,
        run.id,
        "artifact.created",
        {
            "artifact_id": artifact.id,
            "file_id": file.id,
            "filename": file.filename,
            "size": file.size,
            "content_type": file.content_type,
            "role": artifact.role,
            "download_url": f"/v1/files/{file.id}/content",
        },
    )
    return _artifact_out(artifact, file)


@router.get("/files/{file_id}", response_model=FileOut)
def internal_get_file(file_id: str, db: Session = Depends(get_db)) -> dict:
    file = db.get(FileObject, file_id)
    if file is None:
        raise HTTPException(status_code=404, detail="file not found")
    return file_dict(file)


@router.get("/files/{file_id}/content")
def internal_get_file_content(file_id: str, db: Session = Depends(get_db)) -> Response:
    file = db.get(FileObject, file_id)
    if file is None:
        raise HTTPException(status_code=404, detail="file not found")
    data = get_storage().get_bytes(file.storage_key)
    return Response(
        content=data,
        media_type=file.content_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(file.filename)}"},
    )


@router.patch("/files/{file_id}", response_model=FileOut)
def internal_patch_file(file_id: str, body: FilePatch, db: Session = Depends(get_db)) -> dict:
    file = db.get(FileObject, file_id)
    if file is None:
        raise HTTPException(status_code=404, detail="file not found")
    if body.meta is not None:
        merged = dict(file.meta or {})
        merged.update(body.meta)
        file.meta = merged
    if body.status is not None:
        if body.status not in {FileStatus.PENDING, FileStatus.READY, FileStatus.REJECTED}:
            raise HTTPException(status_code=400, detail=f"bad file status: {body.status}")
        file.status = body.status
    db.commit()
    db.refresh(file)
    return file_dict(file)
