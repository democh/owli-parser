"""Storage 端口：StorageBackend / MultipartStorage / SignedReadStorage。

- StorageCapabilities 明确 range、multipart、presigned_read、versioning 等；
  可选能力用独立协议，不用“全部 NotImplemented 再标已支持”。
- ObjectLocator 至少含 profile_revision_id、bucket/root_ref、key、object_version。
- BinaryReader 支持显式 close / context manager，异常与取消也释放连接。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import IO, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class StorageCapabilities:
    range_read: bool
    multipart: bool
    presigned_read: bool
    versioning: bool
    server_side_copy: bool


@dataclass(frozen=True, slots=True)
class ObjectTarget:
    """写入目标。key 由服务端生成，包含租户域，不得由用户提交。"""

    profile_revision_id: uuid.UUID
    bucket: str | None
    root_ref: str | None
    key: str


@dataclass(frozen=True, slots=True)
class ObjectLocator:
    profile_revision_id: uuid.UUID
    bucket: str | None
    root_ref: str | None
    key: str
    object_version: str | None = None


@dataclass(frozen=True, slots=True)
class ByteRange:
    start: int
    end_inclusive: int

    @property
    def length(self) -> int:
        return self.end_inclusive - self.start + 1


@dataclass(frozen=True, slots=True)
class PutOptions:
    content_type: str | None = None
    expected_sha256: str | None = None
    max_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class StoredObject:
    locator: ObjectLocator
    size: int
    sha256: str | None
    provider_etag: str | None


@dataclass(frozen=True, slots=True)
class ObjectStat:
    exists: bool
    size: int | None = None
    provider_etag: str | None = None
    object_version: str | None = None
    content_type: str | None = None


@dataclass(frozen=True, slots=True)
class DeleteResult:
    deleted: bool
    already_absent: bool = False


@dataclass(frozen=True, slots=True)
class MultipartHandle:
    upload_id: str
    target: ObjectTarget
    part_size_bytes: int


@dataclass(frozen=True, slots=True)
class PartReceipt:
    part_no: int
    size: int
    etag: str | None = None
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class SignedReadOptions:
    ttl_seconds: int
    content_disposition: str | None = None


@dataclass(frozen=True, slots=True)
class SignedRead:
    url: str
    expires_at_epoch: int


class BinaryReader(AbstractContextManager["BinaryReader"]):
    """有界读取器；调用方必须 close（或 with 语句），异常/取消同样释放。"""

    def read(self, size: int = -1) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class StreamingReader(BinaryReader):
    """基于文件对象的默认读取实现。"""

    def __init__(self, fileobj: IO[bytes]) -> None:
        self._fileobj = fileobj

    def read(self, size: int = -1) -> bytes:
        return self._fileobj.read(size)

    def close(self) -> None:
        self._fileobj.close()


@runtime_checkable
class StorageBackend(Protocol):
    def capabilities(self) -> StorageCapabilities: ...

    def put(self, target: ObjectTarget, chunks: Iterable[bytes], options: PutOptions) -> StoredObject: ...

    def stat(self, locator: ObjectLocator) -> ObjectStat: ...

    def open_reader(self, locator: ObjectLocator, byte_range: ByteRange | None) -> BinaryReader: ...

    def delete(self, locator: ObjectLocator) -> DeleteResult: ...


@runtime_checkable
class MultipartStorage(Protocol):
    def initiate(self, target: ObjectTarget) -> MultipartHandle: ...

    def upload_part(self, handle: MultipartHandle, part_no: int, chunks: Iterable[bytes]) -> PartReceipt: ...

    def complete(self, handle: MultipartHandle, parts: list[PartReceipt]) -> StoredObject: ...

    def abort(self, handle: MultipartHandle) -> None: ...


@runtime_checkable
class SignedReadStorage(Protocol):
    def presign_read(self, locator: ObjectLocator, options: SignedReadOptions) -> SignedRead: ...


class StorageError(Exception):
    """存储端口统一错误基类（子类映射稳定 ErrorCode）。"""


class StorageNotFound(StorageError):
    pass


class StoragePermissionDenied(StorageError):
    pass


class StorageUnavailable(StorageError):
    pass


class StorageQuotaExceeded(StorageError):
    pass


class StorageChecksumMismatch(StorageError):
    pass


class UnsupportedCapability(StorageError):
    def __init__(self, capability: str) -> None:
        super().__init__(f"存储后端不支持能力: {capability}")
        self.capability = capability


@dataclass(frozen=True, slots=True)
class LocalRootGrant:
    """平台批准的本地存储 root（租户不能提交任意本机路径）。"""

    id: str
    path: str
    quota_bytes: int | None = None
    metadata: dict[str, str] = field(default_factory=dict)
