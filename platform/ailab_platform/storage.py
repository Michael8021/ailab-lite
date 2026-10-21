"""Object storage abstraction.

Dev uses a local directory; T4 swaps in SeaweedFS/S3 with the same interface.
Business code only deals in opaque keys such as ``runs/<run>/<file>/x.pptx``.
"""

from __future__ import annotations

import os
import shutil
import uuid
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path, PurePosixPath

from .config import settings


class Storage(ABC):
    @abstractmethod
    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None: ...

    @abstractmethod
    def get_bytes(self, key: str) -> bytes: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def copy(self, src_key: str, dst_key: str) -> None:
        """Copy an object, overwriting dst. Idempotent, so callers can retry."""

    def commit(self, staging_key: str, final_key: str) -> None:
        """Make a staged object visible at its final key (copy, then drop the staging object)."""
        self.copy(staging_key, final_key)
        self.delete(staging_key)

    def presign_put(self, key: str, expires: int = 900) -> str | None:
        return None

    def presign_get(self, key: str, expires: int = 900) -> str | None:
        return None


def _safe_key(key: str) -> str:
    if not key or key.startswith("/") or "\\" in key:
        raise ValueError(f"invalid storage key: {key!r}")
    parts = PurePosixPath(key).parts
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"invalid storage key: {key!r}")
    return "/".join(parts)


class LocalStorage(Storage):
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / _safe_key(key)).resolve()
        if self.root not in path.parents and path != self.root:
            raise ValueError(f"storage key escapes root: {key!r}")
        return path

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        path = self._path(key)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def copy(self, src_key: str, dst_key: str) -> None:
        src = self._path(src_key)
        dst = self._path(dst_key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.name}.{uuid.uuid4().hex}.tmp")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)


class S3Storage(Storage):
    def __init__(self) -> None:
        import boto3

        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint or None,
            aws_access_key_id=settings.s3_access_key or None,
            aws_secret_access_key=settings.s3_secret_key or None,
            region_name=settings.s3_region,
        )

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        import io

        self.client.put_object(Bucket=self.bucket, Key=_safe_key(key), Body=io.BytesIO(data), ContentType=content_type)

    def get_bytes(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=_safe_key(key))["Body"].read()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=_safe_key(key))

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=_safe_key(key))
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def copy(self, src_key: str, dst_key: str) -> None:
        self.client.copy_object(
            Bucket=self.bucket,
            Key=_safe_key(dst_key),
            CopySource={"Bucket": self.bucket, "Key": _safe_key(src_key)},
        )

    def presign_put(self, key: str, expires: int = 900) -> str | None:
        return self.client.generate_presigned_url("put_object", Params={"Bucket": self.bucket, "Key": _safe_key(key)}, ExpiresIn=expires)

    def presign_get(self, key: str, expires: int = 900) -> str | None:
        return self.client.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": _safe_key(key)}, ExpiresIn=expires)


@lru_cache
def get_storage() -> Storage:
    if settings.storage_backend == "s3":
        return S3Storage()
    return LocalStorage(settings.storage_path)


def reset_storage() -> None:
    get_storage.cache_clear()
