"""Lease lifecycle: provider expiry is derived from the lease, expired leases get reaped."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ailab_platform.broker.service import LeaseManager
from ailab_platform.sandbox.mock import MockSandbox

from .fakes import FakeRedis


class _Platform:
    def append_event(self, *_a, **_k):
        return {}

    def get_run(self, run_id):
        return {"id": run_id, "input_file_ids": []}


class _SpyProvider(MockSandbox):
    def __init__(self) -> None:
        super().__init__()
        self.acquired_ttl: list[int | None] = []
        self.released: list[str] = []
        self.reaped = 0

    def acquire(self, lease_id, image, ttl_seconds=None):
        self.acquired_ttl.append(ttl_seconds)
        return super().acquire(lease_id, image, ttl_seconds)

    def release(self, sandbox_id):
        self.released.append(sandbox_id)
        super().release(sandbox_id)

    def reap_expired(self):
        self.reaped += 1
        return 0


def _manager() -> tuple[LeaseManager, _SpyProvider, FakeRedis]:
    provider, fake = _SpyProvider(), FakeRedis()
    manager = LeaseManager(provider=provider, platform=_Platform(), storage=object(), redis_client=fake)
    return manager, provider, fake


def test_provider_gets_the_lease_ttl() -> None:
    manager, provider, _ = _manager()
    manager.create("run_a", ttl_seconds=120)
    assert provider.acquired_ttl == [120]


def test_expired_lease_is_reaped_even_after_its_redis_ttl_elapsed() -> None:
    manager, provider, fake = _manager()
    lease = manager.create("run_b", ttl_seconds=60)

    # The lease is now past its expiry. The record has to outlive that moment,
    # otherwise cleanup_once (which finds leases by reading them) never sees it.
    lease["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    manager._save(lease)
    fake.advance(2)

    manager.cleanup_once()

    assert provider.released == [lease["sandbox_id"]]
    assert manager.active_leases() == []
    assert provider.reaped == 1  # the provider-side backstop is swept on every pass


# ---- path enforcement, size limits, input injection ------------------------------------

import pytest  # noqa: E402

from ailab_platform.broker.service import ArtifactRejected, LeaseError  # noqa: E402
from ailab_platform.config import settings  # noqa: E402
from ailab_platform.sandbox import SandboxError  # noqa: E402


class _Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_bytes(self, key, data, content_type="x"):
        self.objects[key] = data


class _ApiStub(_Platform):
    def __init__(self, files: dict[str, dict], run_inputs: list[str]) -> None:
        self.files, self.run_inputs, self.committed = files, run_inputs, []

    def get_run(self, run_id):
        return {"id": run_id, "input_file_ids": self.run_inputs}

    def get_file(self, file_id):
        return self.files[file_id]

    def get_file_bytes(self, file_id):
        return self.files[file_id]["bytes"]

    def commit_artifact(self, payload):
        self.committed.append(payload)
        return {"artifact_id": "art_1", "file_id": "fil_1", "filename": payload["filename"],
                "content_type": payload["content_type"], "size": payload["size"], "sha256": payload["sha256"]}


def _manager_with(api: _ApiStub):
    provider, fake, storage = _SpyProvider(), FakeRedis(), _Storage()
    return LeaseManager(provider=provider, platform=api, storage=storage, redis_client=fake), provider, storage


def test_inputs_come_from_the_run_not_from_the_caller() -> None:
    api = _ApiStub(
        {"f1": {"status": "ready", "filename": "a.txt", "bytes": b"one"},
         "f2": {"status": "ready", "filename": "a.txt", "bytes": b"two"},
         "other": {"status": "ready", "filename": "secret.txt", "bytes": b"nope"}},
        run_inputs=["f1", "f2"],
    )
    manager, provider, _ = _manager_with(api)
    lease = manager.create("run_x", inputs=[{"file_id": "other"}])  # caller tries to smuggle a file

    assert [i["path"] for i in lease["inputs"]] == ["/workspace/inputs/a.txt", "/workspace/inputs/a (2).txt"]
    assert provider.get_file(lease["sandbox_id"], "/workspace/inputs/a.txt") == b"one"
    assert provider.get_file(lease["sandbox_id"], "/workspace/inputs/a (2).txt") == b"two"
    with pytest.raises(SandboxError):
        provider.get_file(lease["sandbox_id"], "/workspace/inputs/secret.txt")


def test_unscanned_input_blocks_the_lease() -> None:
    api = _ApiStub({"f1": {"status": "pending", "filename": "a.txt", "bytes": b"x"}}, run_inputs=["f1"])
    manager, _provider, _ = _manager_with(api)
    with pytest.raises(LeaseError, match="pending"):
        manager.create("run_y")
    assert manager.active_leases() == []


def _lease_with_files(files: dict[str, bytes]):
    api = _ApiStub({}, [])
    manager, provider, storage = _manager_with(api)
    lease = manager.create("run_z")
    for path, data in files.items():
        provider.put_file(lease["sandbox_id"], path, data)
    return manager, lease, api, storage


def test_only_outputs_can_be_delivered() -> None:
    manager, lease, api, _ = _lease_with_files({"/workspace/outline.json": b"{}", "/workspace/outputs/a.md": b"hi"})
    with pytest.raises(SandboxError):
        manager.commit(lease["lease_id"], "/workspace/outline.json")
    with pytest.raises(SandboxError):
        manager.commit(lease["lease_id"], "/workspace/outputs/../outline.json")
    with pytest.raises(SandboxError):
        manager.commit(lease["lease_id"], "/workspace/outputs")
    out = manager.commit(lease["lease_id"], "/workspace/outputs/a.md")
    assert out["filename"] == "a.md" and api.committed[0]["size"] == 2


def test_oversized_artifact_is_rejected_before_it_is_read(monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_artifact_bytes", 10)
    manager, lease, api, storage = _lease_with_files({"/workspace/outputs/big.bin": b"x" * 11})
    with pytest.raises(ArtifactRejected):
        manager.commit(lease["lease_id"], "/workspace/outputs/big.bin")
    assert storage.objects == {} and api.committed == []


def test_path_traversal_is_refused_on_every_file_operation() -> None:
    manager, lease, _, _ = _lease_with_files({})
    lid = lease["lease_id"]
    for call in (
        lambda: manager.put_file(lid, "/workspace-evil/x", b"x"),
        lambda: manager.put_file(lid, "/etc/cron.d/x", b"x"),
        lambda: manager.get_file(lid, "/workspace/../etc/passwd"),
        lambda: manager.list_files(lid, "/"),
    ):
        with pytest.raises(SandboxError):
            call()


def test_read_and_write_size_limits(monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_sandbox_read_bytes", 5)
    monkeypatch.setattr(settings, "max_sandbox_write_bytes", 5)
    manager, lease, _, _ = _lease_with_files({"/workspace/big.txt": b"x" * 6})
    with pytest.raises(SandboxError):
        manager.get_file(lease["lease_id"], "/workspace/big.txt")
    with pytest.raises(SandboxError):
        manager.put_file(lease["lease_id"], "/workspace/new.txt", b"y" * 6)


# ---- expiry on read, typed capacity errors, staging cleanup ------------------------------------

from ailab_platform.broker.service import CapacityError  # noqa: E402


def test_expired_lease_is_unusable_even_while_its_record_lingers() -> None:
    manager, provider, _fake = _manager()
    lease = manager.create("run_e", ttl_seconds=60)
    lease["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    manager._save(lease)

    for call in (
        lambda: manager.exec(lease["lease_id"], "echo hi"),
        lambda: manager.put_file(lease["lease_id"], "/workspace/a", b"x"),
        lambda: manager.get(lease["lease_id"]),
    ):
        with pytest.raises(LeaseError, match="expired"):
            call()
    # Asking again for the same run frees the expired sandbox first, then leases a new one (capacity is 1).
    assert manager.create("run_e", ttl_seconds=60)["lease_id"] != lease["lease_id"]
    assert lease["sandbox_id"] in provider.released


def test_capacity_is_a_typed_error_not_a_string_match(monkeypatch) -> None:
    manager, provider, _ = _manager()
    monkeypatch.setattr(settings, "max_sandboxes", 1)
    manager.create("run_1")
    with pytest.raises(CapacityError):
        manager.create("run_2")

    def no_room(*_a, **_k):
        raise SandboxError("insufficient capacity on node")  # a provider fault that merely says "capacity"

    monkeypatch.setattr(settings, "max_sandboxes", 5)
    monkeypatch.setattr(provider, "acquire", no_room)
    with pytest.raises(LeaseError) as exc:
        manager.create("run_3")
    assert not isinstance(exc.value, CapacityError)


def test_failed_artifact_commit_does_not_leak_the_staging_object() -> None:
    class Failing(_ApiStub):
        def commit_artifact(self, payload):
            raise RuntimeError("api down")

    api = Failing({}, [])
    manager, provider, storage = _manager_with(api)
    lease = manager.create("run_f")
    provider.put_file(lease["sandbox_id"], "/workspace/outputs/a.md", b"hi")

    class Deletable(_Storage):
        def delete(self, key):
            self.objects.pop(key, None)

    manager.storage = storage = Deletable()
    with pytest.raises(RuntimeError):
        manager.commit(lease["lease_id"], "/workspace/outputs/a.md")
    assert storage.objects == {}
