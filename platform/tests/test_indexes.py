from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import inspect

from ailab_platform.api.main import app
from ailab_platform.db import engine


def test_performance_indexes_are_created() -> None:
    with TestClient(app):
        inspector = inspect(engine)
        names = {index["name"] for table in ("runs", "files", "conversations", "model_usage") for index in inspector.get_indexes(table)}
    assert {
        "ix_runs_user_created",
        "ix_files_user_created",
        "ix_conversations_user_updated",
        "ix_model_usage_run_created",
    } <= names
