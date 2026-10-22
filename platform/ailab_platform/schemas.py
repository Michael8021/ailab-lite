"""Pydantic API schemas."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


class UTCModel(BaseModel):
    @field_serializer("*", when_used="json")
    def serialize_utc(self, value):
        # SQLite drops tzinfo on reads. All database timestamps are UTC, so expose that explicitly:
        # browsers otherwise parse a naive ISO string in the visitor's local timezone.
        if isinstance(value, datetime):
            return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return value


class ORMModel(UTCModel):
    model_config = ConfigDict(from_attributes=True)


class UserOut(ORMModel):
    id: str
    email: str
    display_name: str


class ArtifactOut(ORMModel):
    id: str
    file_id: str
    role: str
    filename: str
    content_type: str
    size: int
    sha256: str
    download_url: str = ""


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)  # the length policy is applied when a password is *set*


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    display_name: str | None = Field(default=None, max_length=120)
    invite_code: str | None = Field(default=None, max_length=200)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


class PromptRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)

    @field_validator("prompt")
    @classmethod
    def require_nonblank_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must contain non-whitespace characters")
        return value


class RunCreate(PromptRequest):
    title: str | None = Field(default=None, max_length=200)
    file_ids: list[str] = Field(default_factory=list)
    model: str | None = Field(default=None, max_length=120)
    mode: Literal["chat", "work"] = "work"  # chat: answer in words only, no tools, never a sandbox
    skill: str | None = Field(default=None, max_length=40)  # a skill the user picked (implies work)


class RunOut(ORMModel):
    id: str
    user_id: str
    conversation_id: str | None = None
    title: str
    prompt: str
    status: str
    model: str
    input_file_ids: list[str]
    options: dict = Field(default_factory=dict)
    summary: str
    error: str
    result: dict
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunDetail(RunOut):
    artifacts: list[ArtifactOut] = Field(default_factory=list)
    input_files: list[dict] = Field(default_factory=list)
    usage: dict = Field(default_factory=dict)


class InternalRunDetail(RunDetail):
    """What the Agent and Broker get: the run plus its conversation context (never sent to browsers)."""

    history: list[dict] = Field(default_factory=list)
    workspace_restore_key: str | None = None
    disabled_skills: list[str] = Field(default_factory=list)
    personal_skills: list[dict] = Field(default_factory=list)


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=120)


class ConversationPatch(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class MessageCreate(PromptRequest):
    file_ids: list[str] = Field(default_factory=list)
    model: str | None = Field(default=None, max_length=120)
    mode: Literal["chat", "work"] = "work"
    skill: str | None = Field(default=None, max_length=40)


class ConversationOut(UTCModel):
    id: str
    title: str
    model: str
    created_at: datetime
    updated_at: datetime
    last_status: str | None = None


class ConversationDetail(ConversationOut):
    turns: list[RunDetail] = Field(default_factory=list)
    previews: list[dict] = Field(default_factory=list)


class EventOut(ORMModel):
    seq: int
    type: str
    payload: dict
    created_at: datetime


class FileOut(ORMModel):
    id: str
    filename: str
    content_type: str
    size: int
    sha256: str
    status: str
    version: int
    meta: dict
    created_at: datetime
    download_url: str = ""


class FilePatch(BaseModel):
    meta: dict | None = None
    status: str | None = None


class InputFile(BaseModel):
    file_id: str
    filename: str | None = None


class LeaseCreate(BaseModel):
    run_id: str
    image: str | None = None
    ttl_seconds: int | None = None
    inputs: list[InputFile] = Field(default_factory=list)


class LeaseOut(BaseModel):
    lease_id: str
    run_id: str
    provider: str
    sandbox_id: str
    expires_at: datetime
    inputs: list[dict] = Field(default_factory=list)
    restored: bool | None = None  # a conversation turn: whether the previous turn's workspace came back
    restore_error: str | None = None


class ExecIn(BaseModel):
    cmd: str = Field(min_length=1, max_length=20000)
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)


class ExecOut(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


class CommitIn(BaseModel):
    path: str
    filename: str | None = None
    role: str = "output"
    content_type: str | None = None


class CommitOut(BaseModel):
    artifact_id: str
    file_id: str
    filename: str
    content_type: str
    size: int
    sha256: str
    download_url: str = ""


class RunStateOut(BaseModel):
    status: str
    finished_at: datetime | None = None


class RunStatusIn(BaseModel):
    status: str
    summary: str | None = None
    error: str | None = None
    result: dict | None = None


class EventIn(BaseModel):
    type: str = Field(min_length=1, max_length=60)
    payload: dict = Field(default_factory=dict)


class UsageIn(BaseModel):
    model: str = ""
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)


class ArtifactIn(BaseModel):
    run_id: str
    staging_key: str
    filename: str
    content_type: str = "application/octet-stream"
    size: int
    sha256: str
    role: str = "output"
