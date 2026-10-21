"""Pydantic API schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
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


class RunCreate(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    title: str | None = Field(default=None, max_length=200)
    file_ids: list[str] = Field(default_factory=list)
    model: str | None = Field(default=None, max_length=120)


class RunOut(ORMModel):
    id: str
    user_id: str
    title: str
    prompt: str
    status: str
    model: str
    input_file_ids: list[str]
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
