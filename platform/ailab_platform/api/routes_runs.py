"""Run lifecycle, SSE events and cancellation."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import metrics
from ..catalog import get_catalog
from ..config import settings
from ..db import SessionLocal
from ..deps import current_user, get_db
from ..events import append_event, list_events
from ..models import FileObject, FileStatus, Run, RunEvent, RunStatus, User
from ..queue import get_queue
from ..quota import enforce_token_quota, usage_summary
from ..run_state import transition_run
from ..schemas import EventOut, RunCreate, RunDetail, RunOut
from .serializers import run_dict

router = APIRouter(prefix="/v1", tags=["runs"])
log = logging.getLogger("ailab.api.runs")


def _get_owned_run(db: Session, run_id: str, user: User) -> Run:
    run = db.get(Run, run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return run


@router.get("/usage")
def my_usage(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return usage_summary(db, user.id)


@router.get("/models")
def list_models(_: User = Depends(current_user)) -> dict:
    """The models this user can choose from. Never exposes upstream URLs, upstream names or keys."""
    catalog = get_catalog()
    default = catalog.default_id()
    return {"default": default, "models": [{**m.public(), "default": m.id == default} for m in catalog.available()]}


@router.post("/runs", response_model=RunDetail, status_code=status.HTTP_201_CREATED)
def create_run(body: RunCreate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    if len(body.prompt) > settings.max_prompt_chars:
        raise HTTPException(status_code=413, detail=f"prompt exceeds {settings.max_prompt_chars} chars")

    catalog = get_catalog()
    model_id = body.model or catalog.default_id()
    spec = catalog.get(model_id)
    if spec is None or not spec.available:
        raise HTTPException(status_code=400, detail=f"model not available: {model_id}")

    enforce_token_quota(db, user.id)

    active = db.scalar(select(func.count()).select_from(Run).where(Run.user_id == user.id, Run.status.in_(RunStatus.ACTIVE)))
    if active >= settings.max_active_runs_per_user:
        metrics.QUOTA_REJECTIONS.labels("active_runs").inc()
        raise HTTPException(status_code=429, detail="too many active runs for this user")

    inputs: list[str] = []
    for file_id in body.file_ids:
        file = db.get(FileObject, file_id)
        if file is None or file.user_id != user.id:
            raise HTTPException(status_code=400, detail=f"unknown file_id: {file_id}")
        if file.status != FileStatus.READY:
            raise HTTPException(status_code=409, detail=f"file {file_id} is {file.status}, not usable yet")
        inputs.append(file.id)

    run = Run(
        user_id=user.id,
        title=(body.title or body.prompt.strip().splitlines()[0])[:200],
        prompt=body.prompt,
        model=model_id,
        input_file_ids=inputs,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    append_event(db, run.id, "run.created", {"title": run.title, "status": run.status, "model": run.model})
    try:
        get_queue().enqueue(settings.run_queue, {"run_id": run.id})
    except Exception as exc:
        log.exception("failed to enqueue run %s", run.id)
        transition_run(db, run.id, RunStatus.FAILED, error=f"queue unavailable: {exc}")
        raise HTTPException(status_code=503, detail="run queue unavailable") from exc
    return run_dict(db, run, detail=True)


@router.get("/runs", response_model=list[RunOut])
def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    runs = db.scalars(select(Run).where(Run.user_id == user.id).order_by(Run.created_at.desc()).limit(limit).offset(offset)).all()
    return [run_dict(db, run) for run in runs]


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    run = _get_owned_run(db, run_id, user)
    return run_dict(db, run, detail=True)


@router.post("/runs/{run_id}/cancel", response_model=RunDetail)
def cancel_run(run_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    run = _get_owned_run(db, run_id, user)
    if run.status in RunStatus.TERMINAL:
        raise HTTPException(status_code=409, detail=f"run already {run.status}")
    run, _ = transition_run(db, run.id, RunStatus.CANCELED, reason="user_requested")
    return run_dict(db, run, detail=True)


@router.get("/runs/{run_id}/events/history", response_model=list[EventOut])
def event_history(
    run_id: str,
    after: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[RunEvent]:
    _get_owned_run(db, run_id, user)
    return list_events(db, run_id, after)


def _poll_events(run_id: str, after: int) -> tuple[list[tuple[int, str, dict]], str]:
    with SessionLocal() as db:
        rows = db.scalars(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.seq > after).order_by(RunEvent.seq).limit(200)).all()
        run = db.get(Run, run_id)
        status = run.status if run else RunStatus.FAILED
        return [(e.seq, e.type, e.payload or {}) for e in rows], status


@router.get("/runs/{run_id}/events")
async def stream_events(
    run_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
) -> StreamingResponse:
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if run is None or run.user_id != user.id:
            raise HTTPException(status_code=404, detail="run not found")

    last_id = after
    if not last_id:
        header = request.headers.get("last-event-id")
        if header and header.isdigit():
            last_id = int(header)

    async def gen():
        nonlocal last_id
        idle = 0
        while True:
            if await request.is_disconnected():
                return
            events, current_status = await asyncio.to_thread(_poll_events, run_id, last_id)
            for seq, type_, payload in events:
                last_id = seq
                data = json.dumps(payload, ensure_ascii=False)
                yield f"id: {seq}\nevent: {type_}\ndata: {data}\n\n"
            if current_status in RunStatus.TERMINAL and not events:
                yield "event: stream.end\ndata: {}\n\n"
                return
            if events:
                idle = 0
            else:
                idle += 1
                if idle % 20 == 0:
                    yield ": ping\n\n"
            await asyncio.sleep(0.4)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
