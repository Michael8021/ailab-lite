"""Conversations: chat threads. Every message starts a run (one turn); the turns share history and a workspace."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import current_user, get_db
from ..models import Conversation, Run, RunStatus, User, utcnow
from ..run_state import transition_run
from ..schemas import ConversationCreate, ConversationDetail, ConversationOut, ConversationPatch, MessageCreate, RunDetail
from .routes_runs import start_run
from .serializers import run_dict

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])

TITLE_CHARS = 40


def _owned(db: Session, conversation_id: str, user: User) -> Conversation:
    conv = db.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user.id or conv.deleted_at is not None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conv


def _turns(db: Session, conv: Conversation) -> list[Run]:
    return list(db.scalars(select(Run).where(Run.conversation_id == conv.id).order_by(Run.created_at, Run.id)))


def conversation_out(conv: Conversation, last_status: str | None = None) -> dict:
    return {
        "id": conv.id,
        "title": conv.title,
        "model": conv.model,
        "created_at": conv.created_at,
        "updated_at": conv.updated_at,
        "last_status": last_status,
    }


def latest_previews(turns: list[Run]) -> list[dict]:
    """The newest preview of every file the conversation produced (a later turn's render replaces an earlier one)."""
    by_file: dict[str, dict] = {}
    for run in turns:
        for preview in (run.result or {}).get("previews") or []:
            if isinstance(preview, dict) and preview.get("filename"):
                by_file.pop(preview["filename"], None)  # re-insert: most recently touched last
                by_file[preview["filename"]] = {**preview, "run_id": run.id}  # preview URLs are per run
    return list(by_file.values())


@router.get("", response_model=list[ConversationOut])
def list_conversations(
    limit: int = Query(default=100, ge=1, le=300),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    last_status = (
        select(Run.status)
        .where(Run.conversation_id == Conversation.id)
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    rows = db.execute(
        select(Conversation, last_status)
        .where(Conversation.user_id == user.id, Conversation.deleted_at.is_(None))
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
    ).all()
    return [conversation_out(conv, last) for conv, last in rows]


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
def create_conversation(body: ConversationCreate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    conv = Conversation(user_id=user.id, title=(body.title or "").strip()[:200], model=body.model or "")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conversation_out(conv)


@router.get("/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    conv = _owned(db, conversation_id, user)
    turns = _turns(db, conv)
    return {
        **conversation_out(conv, turns[-1].status if turns else None),
        "turns": [run_dict(db, run, detail=True) for run in turns],
        "previews": latest_previews(turns),
    }


@router.patch("/{conversation_id}", response_model=ConversationOut)
def rename_conversation(
    conversation_id: str, body: ConversationPatch, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> dict:
    conv = _owned(db, conversation_id, user)
    conv.title = body.title.strip()[:200]
    db.commit()
    return conversation_out(conv)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(conversation_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    """Hidden from the list (the runs and the files they produced stay in the library). A running turn is canceled."""
    conv = _owned(db, conversation_id, user)
    for run in _turns(db, conv):
        if run.status in RunStatus.ACTIVE:
            transition_run(db, run.id, RunStatus.CANCELED, reason="conversation_deleted")
    conv.deleted_at = utcnow()
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{conversation_id}/messages", response_model=RunDetail, status_code=status.HTTP_201_CREATED)
def send_message(conversation_id: str, body: MessageCreate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    conv = _owned(db, conversation_id, user)
    if not conv.title:
        first_line = body.prompt.strip().splitlines()[0] if body.prompt.strip() else "新对话"
        conv.title = first_line[:TITLE_CHARS] + ("…" if len(first_line) > TITLE_CHARS else "")
    run = start_run(db, user, body.prompt, body.model, body.file_ids, conversation=conv, mode=body.mode, skill=body.skill)
    return run_dict(db, run, detail=True)
