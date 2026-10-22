"""Run lifecycle, SSE events and cancellation."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import metrics
from ..agent.prompts import skills
from ..catalog import get_catalog
from ..config import settings
from ..db import SessionLocal
from ..deps import current_user, current_user_short, get_db
from ..events import append_event, list_events
from ..locks import user_critical_section
from ..models import Conversation, FileObject, FileStatus, Run, RunEvent, RunStatus, User, utcnow
from ..queue import get_queue
from ..quota import enforce_token_quota, usage_summary
from ..run_state import transition_run
from ..schemas import EventOut, RunCreate, RunDetail, RunOut
from ..storage import get_storage
from .serializers import run_dict

router = APIRouter(prefix="/v1", tags=["runs"])
log = logging.getLogger("ailab.api.runs")


def _get_owned_run(db: Session, run_id: str, user: User) -> Run:
    run = db.get(Run, run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return run


def _owns_run(run_id: str, user_id: str) -> bool:
    """Ownership check on a throwaway session, safe to call from a worker thread."""
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        return run is not None and run.user_id == user_id


@router.get("/usage")
def my_usage(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return usage_summary(db, user.id)


@router.get("/models")
def list_models(_: User = Depends(current_user)) -> dict:
    """The models this user can choose from. Never exposes upstream URLs, upstream names or keys."""
    catalog = get_catalog()
    default = catalog.default_id()
    return {"default": default, "models": [{**m.public(), "default": m.id == default} for m in catalog.available()]}


def start_run(
    db: Session,
    user: User,
    prompt: str,
    model: str | None,
    file_ids: list[str],
    title: str | None = None,
    conversation: Conversation | None = None,
    mode: str = "work",
    skill: str | None = None,
) -> Run:
    """Check the limits, write the run and queue it. Used for standalone runs and conversation turns."""
    if len(prompt) > settings.max_prompt_chars:
        raise HTTPException(status_code=413, detail=f"prompt exceeds {settings.max_prompt_chars} chars")
    options: dict = {"mode": mode}
    if skill:
        if skill not in skills():
            raise HTTPException(status_code=400, detail=f"unknown skill: {skill}")
        options = {"mode": "work", "skill": skill}  # a skill is work by definition

    catalog = get_catalog()
    model_id = model or catalog.default_id()
    spec = catalog.get(model_id)
    if spec is None or not spec.available:
        raise HTTPException(status_code=400, detail=f"model not available: {model_id}")

    with user_critical_section(db, user.id):  # the limits are checked and the run written as one step
        enforce_token_quota(db, user.id)

        active = db.scalar(select(func.count()).select_from(Run).where(Run.user_id == user.id, Run.status.in_(RunStatus.ACTIVE)))
        if active >= settings.max_active_runs_per_user:
            metrics.QUOTA_REJECTIONS.labels("active_runs").inc()
            raise HTTPException(status_code=429, detail="too many active runs for this user")
        if conversation is not None:
            busy = db.scalar(
                select(func.count()).select_from(Run).where(Run.conversation_id == conversation.id, Run.status.in_(RunStatus.ACTIVE))
            )
            if busy:
                raise HTTPException(status_code=409, detail="this conversation is still answering the previous message")

        inputs: list[str] = []
        for file_id in file_ids:
            file = db.get(FileObject, file_id)
            if file is None or file.user_id != user.id:
                raise HTTPException(status_code=400, detail=f"unknown file_id: {file_id}")
            if file.status != FileStatus.READY:
                raise HTTPException(status_code=409, detail=f"file {file_id} is {file.status}, not usable yet")
            inputs.append(file.id)

        run = Run(
            user_id=user.id,
            conversation_id=conversation.id if conversation is not None else None,
            title=(title or prompt.strip().splitlines()[0])[:200],
            prompt=prompt,
            model=model_id,
            input_file_ids=inputs,
            options=options,
        )
        db.add(run)
        if conversation is not None:
            conversation.model = model_id
            conversation.updated_at = utcnow()
        db.commit()
        db.refresh(run)

    append_event(db, run.id, "run.created", {"title": run.title, "status": run.status, "model": run.model})
    try:
        get_queue().enqueue(settings.run_queue, {"run_id": run.id})
    except Exception as exc:
        log.exception("failed to enqueue run %s", run.id)
        transition_run(db, run.id, RunStatus.FAILED, error=f"queue unavailable: {exc}")
        raise HTTPException(status_code=503, detail="run queue unavailable") from exc
    return run


@router.post("/runs", response_model=RunDetail, status_code=status.HTTP_201_CREATED)
def create_run(body: RunCreate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    run = start_run(db, user, body.prompt, body.model, body.file_ids, body.title, mode=body.mode, skill=body.skill)
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


PREVIEW_NAME = re.compile(r"^[0-9]{1,4}/(p-[0-9]{1,3}\.jpg|sheets\.json)$")


@router.get("/runs/{run_id}/previews/{name:path}")
def get_preview(run_id: str, name: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    """A rendered page (JPEG) or sheet data (JSON) of a file the run produced. Written by the Broker."""
    _get_owned_run(db, run_id, user)
    if not PREVIEW_NAME.match(name):
        raise HTTPException(status_code=404, detail="no such preview")
    try:
        data = get_storage().get_bytes(f"previews/{run_id}/{name}")
    except Exception as exc:
        raise HTTPException(status_code=404, detail="no such preview") from exc
    media = "image/jpeg" if name.endswith(".jpg") else "application/json"
    # immutable: every render gets a new number
    return Response(content=data, media_type=media, headers={"Cache-Control": "private, max-age=86400, immutable"})


SSE_FAST_POLLS = 8  # ~2 s of quiet at the fast rate before backing off


def sse_poll_delay(idle_polls: int) -> float:
    """Seconds until the next poll of a run's events.

    Fast (0.25 s) while events flow and for ~2 s after the last one: a streamed answer arrives in batches about
    0.25 s apart, so a single empty poll in between must not drop the stream to the slow rate and make it
    stutter. After that, 1 s: 200 idle viewers at 4 polls/s would be 800 DB queries/s. Jitter spreads them out.
    """
    base = 0.25 if idle_polls < SSE_FAST_POLLS else 1.0
    return base + random.uniform(0, 0.05 if base < 1 else 0.15)


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
    user: User = Depends(current_user_short),
) -> StreamingResponse:
    # Do not keep a Session (or its pool connection) open for the stream: the
    # dependency above closes its session immediately. The ownership check is a
    # short query, always run off the event loop.
    if not await asyncio.to_thread(_owns_run, run_id, user.id):
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
            await asyncio.sleep(sse_poll_delay(idle))

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
