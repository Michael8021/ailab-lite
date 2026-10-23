"""Response serialization helpers (kept out of route modules for reuse)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Artifact, FileObject, ModelUsage, Run


def file_dict(file: FileObject) -> dict:
    return {
        "id": file.id,
        "project_id": file.project_id,
        "starred": file.starred_at is not None,
        "filename": file.filename,
        "content_type": file.content_type,
        "size": file.size,
        "sha256": file.sha256,
        "status": file.status,
        "version": file.version,
        "meta": file.meta or {},
        "created_at": file.created_at,
        "download_url": f"/v1/files/{file.id}/content",
        "thumbnail_url": f"/v1/files/{file.id}/thumbnail" if (file.meta or {}).get("thumb_key") else None,
    }


def artifact_dict(artifact: Artifact, file: FileObject) -> dict:
    return {
        "id": artifact.id,
        "file_id": file.id,
        "role": artifact.role,
        "filename": file.filename,
        "content_type": file.content_type,
        "size": file.size,
        "sha256": file.sha256,
        "download_url": f"/v1/files/{file.id}/content",
    }


def run_dict(db: Session, run: Run, detail: bool = False) -> dict:
    out = {
        "id": run.id,
        "user_id": run.user_id,
        "conversation_id": run.conversation_id,
        "title": run.title,
        "prompt": run.prompt,
        "status": run.status,
        "model": run.model,
        "input_file_ids": run.input_file_ids or [],
        "options": run.options or {},
        "summary": run.summary,
        "error": run.error,
        "result": run.result or {},
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }
    if detail:
        rows = db.execute(
            select(Artifact, FileObject).join(FileObject, Artifact.file_id == FileObject.id).where(Artifact.run_id == run.id)
        ).all()
        out["artifacts"] = [artifact_dict(a, f) for a, f in rows]
        out["input_files"] = [
            {"file_id": f.id, "filename": f.filename}
            for f in db.scalars(select(FileObject).where(FileObject.id.in_(run.input_file_ids or [])))
        ]
        usage = db.execute(select(ModelUsage).where(ModelUsage.run_id == run.id)).scalars().all()
        out["usage"] = {
            "prompt_tokens": sum(u.prompt_tokens for u in usage),
            "completion_tokens": sum(u.completion_tokens for u in usage),
            "cached_tokens": sum(u.cached_tokens for u in usage),
            "calls": len(usage),
        }
    return out
