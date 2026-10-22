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

from .. import metrics
from ..catalog import get_catalog
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
    InternalRunDetail,
    RunDetail,
    RunStateOut,
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


ATTACHMENT_CHARS = 30_000  # of one attachment's text given to the model
ATTACHMENTS_CHARS = 60_000  # of all of a run's attachments together


def _attachment_texts(db: Session, run: Run) -> list[dict]:
    """The run's input files, each with the text the Worker extracted (so the model can read it without a sandbox)."""
    storage = get_storage()
    out, budget = [], ATTACHMENTS_CHARS
    for file_id in run.input_file_ids or []:
        f = db.get(FileObject, file_id)
        if f is None:
            continue
        meta = f.meta or {}
        text = ""
        if meta.get("text_key"):
            try:
                text = storage.get_bytes(meta["text_key"]).decode("utf-8", "replace")
            except Exception:
                log.warning("attachment text missing for %s", file_id, exc_info=True)
        total = len(text) or int((meta.get("extract") or {}).get("chars") or 0)
        if not text and meta.get("excerpt"):  # processed before full texts were kept: the excerpt is all there is
            text = meta["excerpt"]
        keep = text[: max(0, min(ATTACHMENT_CHARS, budget))]
        budget -= len(keep)
        out.append(
            {
                "file_id": f.id,
                "filename": f.filename,
                "kind": (meta.get("extract") or {}).get("kind", ""),
                "chars": total,
                "text": keep,
                "truncated": len(keep) < total,
            }
        )
    return out


HISTORY_TURNS = 12
HISTORY_CHARS = 4000  # per reply: older answers are context, not something to repeat verbatim


def workspace_key(run_id: str) -> str:
    """Where the Broker keeps the sandbox workspace a turn left behind (derived, never taken from a caller)."""
    return f"workspaces/{run_id}.tgz"


def _earlier_turns(db: Session, run: Run) -> list[Run]:
    if not run.conversation_id:
        return []
    return list(
        db.scalars(
            select(Run)
            .where(Run.conversation_id == run.conversation_id, Run.created_at <= run.created_at, Run.id != run.id)
            .order_by(Run.created_at, Run.id)
        )
    )


@router.get("/runs/{run_id}", response_model=InternalRunDetail)
def internal_get_run(run_id: str, db: Session = Depends(get_db)) -> dict:
    """The run, and for a conversation turn: the earlier turns (for the model) and the workspace to restore (for the Broker)."""
    run = _run_or_404(db, run_id)
    out = run_dict(db, run, detail=True)
    earlier = _earlier_turns(db, run)
    history = []
    for turn in earlier[-HISTORY_TURNS:]:
        reply = turn.summary or (f"（这一轮失败了：{turn.error}）" if turn.error else "")
        if turn.status == RunStatus.CANCELED:
            reply = (reply + "\n" if reply else "") + "（用户中止了这一轮）"
        files = [
            f.filename
            for f in db.scalars(select(FileObject).join(Artifact, Artifact.file_id == FileObject.id).where(Artifact.run_id == turn.id))
        ]
        history.append({"prompt": turn.prompt, "reply": reply[:HISTORY_CHARS], "status": turn.status, "files": files})
    out["history"] = history
    out["input_files"] = _attachment_texts(db, run)
    out["workspace_restore_key"] = next(
        (workspace_key(t.id) for t in reversed(earlier) if (t.result or {}).get("workspace_saved")),
        None,
    )
    return out


@router.get("/runs/{run_id}/state", response_model=RunStateOut)
def internal_run_state(run_id: str, db: Session = Depends(get_db)) -> dict:
    """The cheap status poll the Agent uses while a command/model call is running."""
    run = _run_or_404(db, run_id)
    return {"status": run.status, "finished_at": run.finished_at}


@router.post("/runs/{run_id}/status", response_model=RunDetail)
def internal_set_status(run_id: str, body: RunStatusIn, db: Session = Depends(get_db)) -> dict:
    _run_or_404(db, run_id)
    if body.status not in {RunStatus.RUNNING, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED}:
        raise HTTPException(status_code=400, detail=f"bad status: {body.status}")
    try:
        run, changed = transition_run(db, run_id, body.status, summary=body.summary, error=body.error, result=body.result)
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=f"invalid transition: {exc}") from exc
    if changed and (run.result or {}).get("workspace_saved"):
        _drop_older_workspaces(db, run)
    return run_dict(db, run, detail=True)


def _drop_older_workspaces(db: Session, run: Run) -> None:
    """Only the newest workspace of a conversation is ever restored; delete the ones before it (best effort)."""
    storage = get_storage()
    for turn in _earlier_turns(db, run):
        result = dict(turn.result or {})
        if not result.get("workspace_saved"):
            continue
        try:
            storage.delete(workspace_key(turn.id))
        except Exception:
            log.warning("could not delete workspace of %s", turn.id, exc_info=True)
            continue
        result["workspace_saved"] = False
        turn.result = result
    db.commit()


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
    model = metrics.model_label(body.model)
    metrics.MODEL_TOKENS.labels(model, "prompt").inc(body.prompt_tokens)
    metrics.MODEL_TOKENS.labels(model, "completion").inc(body.completion_tokens)
    metrics.MODEL_TOKENS.labels(model, "cached").inc(min(body.cached_tokens, body.prompt_tokens))
    db.add(
        ModelUsage(
            run_id=run_id,
            model=body.model,
            prompt_tokens=body.prompt_tokens,
            completion_tokens=body.completion_tokens,
            cached_tokens=min(body.cached_tokens, body.prompt_tokens),
            # Frozen now: a later edit of models.toml must not change what this call cost.
            weight=get_catalog().weight_of(body.model),
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
