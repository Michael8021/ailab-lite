"""Test bootstrap: point every adapter at an isolated temp profile."""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="ailab-platform-test-")

# Never read the developer's platform/.env: it may hold a real model key and a different model name.
os.environ["AILAB_ENV_FILE"] = os.path.join(_TMP, "no-such.env")
os.environ.setdefault("AILAB_ENV", "test")
os.environ["AILAB_DATA_DIR"] = _TMP
os.environ["AILAB_STORAGE_ROOT"] = os.path.join(_TMP, "storage")
# AILAB_TEST_DATABASE_URL runs the whole suite on another database (a throwaway PostgreSQL); it is wiped first.
_DB = os.environ.get("AILAB_TEST_DATABASE_URL")
if _DB:
    from sqlalchemy import create_engine, text

    _eng = create_engine(_DB)
    with _eng.begin() as _conn:
        _conn.execute(text("DROP SCHEMA public CASCADE"))
        _conn.execute(text("CREATE SCHEMA public"))
    _eng.dispose()
os.environ["AILAB_DATABASE_URL"] = _DB or f"sqlite:///{os.path.join(_TMP, 'test.db')}"
os.environ["AILAB_QUEUE_BACKEND"] = "memory"
os.environ["AILAB_SANDBOX_PROVIDER"] = "mock"
os.environ["AILAB_MODELS_FILE"] = os.path.join(os.path.dirname(__file__), "models.test.toml")
os.environ["AILAB_DEV_TOKEN"] = "test-token"
os.environ["AILAB_SERVICE_TOKEN"] = "test-service"
os.environ["AILAB_REDIS_URL"] = "redis://127.0.0.1:6379/15"
os.environ["AILAB_MAX_ACTIVE_RUNS_PER_USER"] = "1000"
