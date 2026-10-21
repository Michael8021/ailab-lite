"""Database models.

Platform API is the only writer; Agent / Broker / Worker go through the
internal API instead of importing this module.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class RunStatus:
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"

    TERMINAL = {SUCCEEDED, FAILED, CANCELED}
    ACTIVE = {QUEUED, RUNNING}


class FileStatus:
    PENDING = "pending"
    READY = "ready"
    REJECTED = "rejected"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("usr"))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tokens: Mapped[list[ApiToken]] = relationship(back_populates="user")


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("tok"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="default")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="tokens")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("run"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    prompt: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.QUEUED, index=True)
    model: Mapped[str] = mapped_column(String(120), default="")
    input_file_ids: Mapped[list] = mapped_column(JSON, default=list)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    events: Mapped[list[RunEvent]] = relationship(back_populates="run", cascade="all, delete-orphan", order_by="RunEvent.seq")
    artifacts: Mapped[list[Artifact]] = relationship(back_populates="run")


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_run_event_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="events")


class FileObject(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("fil"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    storage_key: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default=FileStatus.PENDING)
    version: Mapped[int] = mapped_column(Integer, default=1)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("files.id"), nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("art"))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id"))
    role: Mapped[str] = mapped_column(String(30), default="output")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="artifacts")
    file: Mapped[FileObject] = relationship()


class ModelUsage(Base):
    __tablename__ = "model_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    model: Mapped[str] = mapped_column(String(120), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Part of prompt_tokens that the upstream served from its prompt cache (billed far lower).
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # The model's quota weight *when the call was recorded*. Changing a weight in models.toml later
    # must not rewrite what past calls cost.
    weight: Mapped[float] = mapped_column(Float, default=1.0, server_default="1.0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
