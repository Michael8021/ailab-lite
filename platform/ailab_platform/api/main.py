"""Platform API entrypoint (sole writer of the business database)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..catalog import get_catalog
from ..config import settings
from ..db import SessionLocal, init_db
from ..deps import current_user
from ..models import User
from ..queue import get_queue
from .reaper import reap_forever
from .routes_files import router as files_router
from .routes_internal import router as internal_router
from .routes_runs import router as runs_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("ailab.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.validate_secrets()
    settings.ensure_dirs()
    get_catalog()  # fail at startup, not on the first request, if models.toml is broken
    init_db()
    log.info("platform api ready db=%s storage=%s", settings.database_url, settings.storage_backend)
    reaper = asyncio.create_task(reap_forever(), name="reaper")
    yield
    reaper.cancel()


app = FastAPI(
    title="AILab Platform API",
    version="0.1.0",
    description="Identity, runs, files, events and the artifact ledger. Sole DB writer.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def reject_oversized_upload(request: Request, call_next):
    """Starlette parses the whole multipart body before the handler runs, so the size
    check inside the handler comes too late to spare the disk. Refuse on Content-Length
    first. (A chunked upload has no Content-Length: cap it at the reverse proxy.)"""
    if request.method == "POST" and request.url.path == "/v1/files":
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > settings.max_upload_bytes + 64 * 1024:
            return JSONResponse(status_code=413, content={"detail": f"file exceeds {settings.max_upload_bytes} bytes"})
    return await call_next(request)


app.include_router(runs_router)
app.include_router(files_router)
app.include_router(internal_router)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "service": "platform-api", "env": settings.env}


@app.get("/readyz")
def readyz() -> dict:
    checks: dict[str, object] = {"db": False, "queue": False, "storage": False}
    code = 200
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        checks["db"] = True
    except Exception as exc:  # pragma: no cover - operational
        checks["db_error"] = str(exc)
        code = 503
    try:
        checks["queue"] = get_queue().ping()
    except Exception as exc:  # pragma: no cover
        checks["queue_error"] = str(exc)
    checks["storage"] = settings.storage_path.exists() if settings.storage_backend == "local" else True
    if not checks["queue"] or not checks["storage"]:
        code = 503
    return JSONResponse(status_code=code, content={"ok": code == 200, "checks": checks})


@app.get("/v1/info")
def info(_: User = Depends(current_user)) -> dict:
    """What this deployment runs on, for the Portal banner. Never include secrets here."""
    return {
        "env": settings.env,
        "default_model": get_catalog().default_id(),
        "sandbox_provider": settings.sandbox_provider,
        "storage_backend": settings.storage_backend,
        "max_upload_bytes": settings.max_upload_bytes,
    }


@app.get("/")
def root() -> dict:
    return {
        "service": "platform-api",
        "version": "0.1.0",
        "docs": "/docs",
        "health": "/healthz",
    }
