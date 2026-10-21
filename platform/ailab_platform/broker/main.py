"""Sandbox Broker HTTP service."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Response, status

from ..config import settings
from ..deps import metrics_auth, service_auth
from ..metrics import BrokerStateCollector, metrics_response, register_once
from ..obs import RequestContextMiddleware, setup_logging
from ..sandbox import SandboxError
from ..schemas import CommitIn, CommitOut, ExecIn, ExecOut, LeaseCreate, LeaseOut
from .service import ArtifactRejected, CapacityError, LeaseError, LeaseManager

setup_logging("broker")
log = logging.getLogger("ailab.broker")

_manager: LeaseManager | None = None


def get_manager() -> LeaseManager:
    global _manager
    if _manager is None:
        _manager = LeaseManager()
    return _manager


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.validate_secrets()
    settings.ensure_dirs()
    manager = get_manager()
    register_once("broker-state", BrokerStateCollector(get_manager))
    try:
        manager.recover()
        manager.start_cleanup_thread()
        log.info("sandbox broker ready provider=%s", manager.provider.name)
    except Exception:
        log.exception("broker startup recovery failed; continuing")
    yield


app = FastAPI(title="AILab Sandbox Broker", version="0.1.0", lifespan=lifespan)
app.add_middleware(RequestContextMiddleware, service="broker")
router = APIRouter(prefix="/internal", tags=["leases"], dependencies=[Depends(service_auth)])


@router.post("/leases", response_model=LeaseOut, status_code=status.HTTP_201_CREATED)
def create_lease(body: LeaseCreate) -> dict:
    manager = get_manager()
    try:
        lease = manager.create(
            run_id=body.run_id,
            inputs=[i.model_dump() for i in body.inputs],
            image=body.image,
            ttl_seconds=body.ttl_seconds,
        )
    except LeaseError as exc:
        raise HTTPException(status_code=429 if isinstance(exc, CapacityError) else 502, detail=str(exc)) from exc
    return {
        "lease_id": lease["lease_id"],
        "run_id": lease["run_id"],
        "provider": lease["provider"],
        "sandbox_id": lease["sandbox_id"],
        "expires_at": lease["expires_at"],
        "inputs": lease.get("inputs", []),
    }


@router.get("/leases")
def list_leases() -> dict:
    return {"leases": get_manager().active_leases()}


@router.get("/leases/{lease_id}")
def get_lease(lease_id: str) -> dict:
    try:
        return get_manager().get(lease_id)
    except LeaseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/leases/{lease_id}/exec", response_model=ExecOut)
def exec_in_sandbox(lease_id: str, body: ExecIn) -> dict:
    try:
        result = get_manager().exec(lease_id, body.cmd, body.timeout_seconds)
    except LeaseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SandboxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return asdict(result)


@router.put("/leases/{lease_id}/files")
def put_file(
    lease_id: str,
    path: str = Query(..., min_length=1),
    payload: bytes = Body(..., media_type="application/octet-stream"),
) -> dict:
    try:
        get_manager().put_file(lease_id, path, payload)
    except LeaseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SandboxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "path": path, "bytes": len(payload)}


@router.get("/leases/{lease_id}/files/list")
def list_files(lease_id: str, path: str = Query(default="/workspace")) -> dict:
    try:
        return {"files": get_manager().list_files(lease_id, path)}
    except LeaseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SandboxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/leases/{lease_id}/files")
def get_file(lease_id: str, path: str = Query(..., min_length=1)) -> Response:
    try:
        data = get_manager().get_file(lease_id, path)
    except LeaseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SandboxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(content=data, media_type="application/octet-stream")


@router.post("/leases/{lease_id}/commit", response_model=CommitOut)
def commit_artifact(lease_id: str, body: CommitIn) -> dict:
    try:
        return get_manager().commit(lease_id, body.path, body.filename, body.role)
    except LeaseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ArtifactRejected as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except SandboxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # API commit failure
        raise HTTPException(status_code=502, detail=f"artifact commit failed: {exc}") from exc


@router.delete("/leases/{lease_id}", status_code=status.HTTP_204_NO_CONTENT)
def release_lease(lease_id: str) -> Response:
    get_manager().release(lease_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


app.include_router(router)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "service": "sandbox-broker", "provider": get_manager().provider.name}


@app.get("/readyz")
def readyz() -> dict:
    manager = get_manager()
    try:
        redis_ok = bool(manager.redis.ping())
    except Exception:
        redis_ok = False
    return {
        "ok": redis_ok and manager.provider.health(),
        "redis": redis_ok,
        "provider": manager.provider.name,
        "provider_healthy": manager.provider.health(),
    }


@app.get("/metrics", include_in_schema=False, dependencies=[Depends(metrics_auth)])
def metrics_endpoint() -> Response:
    return metrics_response()
