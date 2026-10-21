"""Sandbox Broker: lease lifecycle, input injection and two-phase output commit."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import redis

from .. import metrics
from ..clients import PlatformClient
from ..config import settings
from ..paths import OUTPUTS, PathError, normalize_path
from ..sandbox import ExecResult, SandboxError, get_provider
from ..storage import get_storage

log = logging.getLogger("ailab.broker")

LEASE_RECORD_GRACE_SECONDS = 3600


class LeaseError(RuntimeError):
    pass


class CapacityError(LeaseError):
    """No free sandbox slot right now; the caller should wait and retry."""


class ArtifactRejected(RuntimeError):
    """The sandbox file cannot be delivered (too large, wrong place)."""


def _safe_name(name: str) -> str:
    return (Path(name.replace("\\", "/")).name or "file")[:200]


def _expired(lease: dict) -> bool:
    return datetime.fromisoformat(lease["expires_at"]) <= datetime.now(UTC)


def _unique_name(name: str, used: set[str]) -> str:
    """Two inputs may share a filename; keep both (a.txt, a (2).txt, ...)."""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    candidate, n = name, 1
    while candidate in used:
        n += 1
        candidate = f"{stem} ({n}){dot}{ext}"
    used.add(candidate)
    return candidate


class LeaseManager:
    def __init__(self, provider=None, platform=None, storage=None, redis_client=None) -> None:
        self.provider = provider or get_provider()
        self.platform = platform or PlatformClient()
        self.storage = storage or get_storage()
        self.redis = redis_client or redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=5)
        self.prefix = "ailab:lease:"
        self.index_key = "ailab:leases:active"
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- lifecycle
    def _key(self, lease_id: str) -> str:
        return f"{self.prefix}{lease_id}"

    def _load(self, lease_id: str, allow_expired: bool = False) -> dict:
        raw = self.redis.get(self._key(lease_id))
        if not raw:
            raise LeaseError(f"lease not found: {lease_id}")
        lease = json.loads(raw)
        # The record outlives expires_at on purpose (see _save), so expiry is checked here.
        if not allow_expired and _expired(lease):
            raise LeaseError(f"lease expired: {lease_id}")
        return lease

    def _save(self, lease: dict) -> None:
        expires_at = datetime.fromisoformat(lease["expires_at"])
        remaining = int((expires_at - datetime.now(UTC)).total_seconds())
        # Keep the record past expiry: cleanup_once finds expired leases by reading
        # them, so a key that vanishes exactly at expires_at would never be reaped.
        ttl = max(1, remaining) + LEASE_RECORD_GRACE_SECONDS
        self.redis.set(self._key(lease["lease_id"]), json.dumps(lease, ensure_ascii=False), ex=ttl)
        self.redis.sadd(self.index_key, lease["lease_id"])

    def _drop(self, lease_id: str) -> None:
        self.redis.delete(self._key(lease_id))
        self.redis.srem(self.index_key, lease_id)

    def active_leases(self) -> list[dict]:
        out: list[dict] = []
        for lease_id in self.redis.smembers(self.index_key):
            raw = self.redis.get(self._key(lease_id))
            if not raw:
                self.redis.srem(self.index_key, lease_id)
                continue
            out.append(json.loads(raw))
        return out

    def create(
        self,
        run_id: str,
        inputs: list[dict] | None = None,  # ignored: the Broker reads the run's inputs from the API
        image: str | None = None,
        ttl_seconds: int | None = None,
    ) -> dict:
        self._release_expired()  # an expired lease still holds a sandbox: free it before counting capacity
        with self._lock:
            existing = next((lease for lease in self.active_leases() if lease["run_id"] == run_id and not _expired(lease)), None)
            if existing:
                return existing
            active = self.active_leases()
            if len(active) >= settings.max_sandboxes:
                metrics.SANDBOX_CAPACITY_REJECTIONS.inc()
                raise CapacityError(f"sandbox capacity reached ({settings.max_sandboxes})")

            started = time.perf_counter()
            lease_id = f"lease_{uuid.uuid4().hex[:16]}"
            try:
                sandbox_id = self.provider.acquire(
                    lease_id, image or settings.sandbox_image, ttl_seconds=ttl_seconds or settings.sandbox_ttl_seconds
                )
            except SandboxError as exc:
                raise LeaseError(f"could not acquire sandbox: {exc}") from exc

            ttl = ttl_seconds or settings.sandbox_ttl_seconds
            now = datetime.now(UTC)
            lease = {
                "lease_id": lease_id,
                "run_id": run_id,
                "provider": self.provider.name,
                "sandbox_id": sandbox_id,
                "image": image or settings.sandbox_image,
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
            }
            self._save(lease)

        # Input injection happens outside the capacity lock: it is I/O bound and
        # only touches this lease's sandbox.
        try:
            # The caller only names the run; which files belong to it is the API's
            # call, so a compromised Agent cannot pull in someone else's files.
            run = self.platform.get_run(run_id)
            used: set[str] = set()
            injected: list[dict] = []
            for file_id in run.get("input_file_ids") or []:
                meta = self.platform.get_file(file_id)
                if meta.get("status") != "ready":
                    raise LeaseError(f"input file {file_id} is {meta.get('status')}")
                data = self.platform.get_file_bytes(file_id)
                name = _unique_name(_safe_name(meta.get("filename") or file_id), used)
                path = f"/workspace/inputs/{name}"
                self.provider.put_file(sandbox_id, path, data)
                injected.append({"file_id": file_id, "filename": name, "path": path})
            lease["inputs"] = injected
            self._save(lease)
        except Exception as exc:
            log.exception("input injection failed for %s", lease_id)
            self.release(lease_id, reason="input_injection_failed")
            raise LeaseError(f"input injection failed: {exc}") from exc

        metrics.SANDBOX_ACQUIRE_SECONDS.observe(time.perf_counter() - started)
        try:
            self.platform.append_event(
                run_id,
                "sandbox.leased",
                {"lease_id": lease_id, "sandbox_id": sandbox_id, "provider": self.provider.name},
            )
        except Exception:
            log.warning("could not record sandbox.leased for %s", run_id, exc_info=True)
        return lease

    def get(self, lease_id: str) -> dict:
        return self._load(lease_id)

    def release(self, lease_id: str, reason: str = "released") -> None:
        try:
            lease = self._load(lease_id, allow_expired=True)
        except LeaseError:
            return
        with self._lock:
            try:
                self.provider.release(lease["sandbox_id"])
            except Exception:
                log.warning("provider release failed for %s", lease_id, exc_info=True)
            self._drop(lease_id)
        try:
            self.platform.append_event(lease["run_id"], "sandbox.released", {"lease_id": lease_id, "reason": reason})
        except Exception:
            pass

    # ---------------------------------------------------------------- sandbox I/O
    def exec(self, lease_id: str, cmd: str, timeout: int | None = None) -> ExecResult:
        lease = self._load(lease_id)
        timeout = timeout or settings.sandbox_exec_timeout_seconds
        with metrics.SANDBOX_EXEC_SECONDS.labels(self.provider.name).time():
            return self.provider.exec(lease["sandbox_id"], cmd, timeout)

    @staticmethod
    def _checked(path: str, under: str = "/workspace") -> str:
        try:
            return normalize_path(path, under)
        except PathError as exc:
            raise SandboxError(str(exc)) from exc

    def put_file(self, lease_id: str, path: str, data: bytes) -> None:
        lease = self._load(lease_id)
        path = self._checked(path)
        if len(data) > settings.max_sandbox_write_bytes:
            raise SandboxError(f"file too large to write ({len(data)} > {settings.max_sandbox_write_bytes} bytes)")
        self.provider.put_file(lease["sandbox_id"], path, data)

    def get_file(self, lease_id: str, path: str) -> bytes:
        lease = self._load(lease_id)
        path = self._checked(path)
        size = self.provider.file_size(lease["sandbox_id"], path)
        if size > settings.max_sandbox_read_bytes:
            raise SandboxError(f"file too large to read ({size} > {settings.max_sandbox_read_bytes} bytes)")
        return self.provider.get_file(lease["sandbox_id"], path)

    def list_files(self, lease_id: str, path: str) -> list[dict]:
        lease = self._load(lease_id)
        return self.provider.list_files(lease["sandbox_id"], self._checked(path))

    def commit(self, lease_id: str, path: str, filename: str | None = None, role: str = "output") -> dict:
        lease = self._load(lease_id)
        path = self._checked(path, OUTPUTS)  # only /workspace/outputs is archived
        if path == OUTPUTS:
            raise SandboxError("deliver a file inside /workspace/outputs, not the directory")
        size = self.provider.file_size(lease["sandbox_id"], path)
        if size > settings.max_artifact_bytes:
            raise ArtifactRejected(f"{path} is {size} bytes; the limit is {settings.max_artifact_bytes}")
        data = self.provider.get_file(lease["sandbox_id"], path)
        name = _safe_name(filename or Path(path).name)
        staging_key = f"staging/{lease_id}/{uuid.uuid4().hex}/{name}"
        self.storage.put_bytes(staging_key, data, mimetypes.guess_type(name)[0] or "application/octet-stream")
        payload = {
            "run_id": lease["run_id"],
            "staging_key": staging_key,
            "filename": name,
            "content_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "role": role,
        }
        try:
            delivered = self.platform.commit_artifact(payload)
            metrics.ARTIFACT_BYTES.inc(len(data))
            return delivered
        except Exception:
            log.exception("artifact commit failed for lease %s staging=%s", lease_id, staging_key)
            # A retry stages a fresh object under a new key, so this one would leak.
            try:
                self.storage.delete(staging_key)
            except Exception:
                log.warning("could not delete staging object %s", staging_key, exc_info=True)
            raise

    # ---------------------------------------------------------------- housekeeping
    def recover(self) -> None:
        """Dev restart policy: drop orphan containers and stale lease records."""
        cleanup = getattr(self.provider, "cleanup_orphans", None)
        if callable(cleanup):
            try:
                cleanup()
            except Exception:
                log.warning("orphan cleanup failed", exc_info=True)
        for key in self.redis.scan_iter(f"{self.prefix}*"):
            self.redis.delete(key)
        self.redis.delete(self.index_key)

    def _release_expired(self) -> None:
        for lease in self.active_leases():
            if _expired(lease):
                log.info("lease %s expired", lease["lease_id"])
                self.release(lease["lease_id"], reason="expired")

    def cleanup_once(self) -> None:
        self._release_expired()
        try:
            reaped = self.provider.reap_expired()
            if reaped:
                log.warning("reaped %d sandbox(es) past provider expiry", reaped)
        except Exception:
            log.warning("provider reap failed", exc_info=True)

    def start_cleanup_thread(self) -> threading.Thread:
        def loop() -> None:
            while True:
                time.sleep(60)
                try:
                    self.cleanup_once()
                except Exception:
                    log.warning("lease cleanup failed", exc_info=True)

        thread = threading.Thread(target=loop, name="lease-cleanup", daemon=True)
        thread.start()
        return thread
