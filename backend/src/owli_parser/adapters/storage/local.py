"""LocalStorage：平台批准 root 下的本地文件存储。

- 服务端流式读写、Range、持久分片；无对象云端签名，下载走应用代理。
- 防 symlink/路径穿越；临时文件 + 完整校验 + 同文件系统原子 rename，
  保证不完整文件不可见；创建与重命名防并发覆盖。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections.abc import Iterable
from pathlib import Path

from owli_parser.core.crypto import StreamingSha256
from owli_parser.ports.storage import (
    BinaryReader,
    ByteRange,
    DeleteResult,
    MultipartHandle,
    ObjectLocator,
    ObjectStat,
    ObjectTarget,
    PartReceipt,
    PutOptions,
    StorageCapabilities,
    StorageChecksumMismatch,
    StorageNotFound,
    StorageQuotaExceeded,
    StorageUnavailable,
    StoredObject,
    StreamingReader,
)

_CHUNK_SIZE = 1024 * 1024


def _safe_join(root: Path, key: str) -> Path:
    """校验 key 形状并拼接到 root；拒绝绝对路径、穿越与可疑字符。"""
    if not key or key.startswith("/") or "\\" in key:
        raise StorageUnavailable("object key 不合法")
    parts = key.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise StorageUnavailable("object key 不合法")
    candidate = root.joinpath(*parts)
    resolved = candidate.resolve(strict=False)
    root_resolved = root.resolve(strict=True)
    if not resolved.is_relative_to(root_resolved):
        raise StorageUnavailable("object key 越出存储根目录")
    return candidate


class LocalStorageBackend:
    """StorageBackend + MultipartStorage（本地持久分片）。

    构造时传入该 profile revision 对应的根目录（平台批准 root）。
    """

    provider = "local"

    def __init__(self, root: Path, quota_bytes: int | None = None) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self._root = root
        self._quota_bytes = quota_bytes

    # -- capabilities ---------------------------------------------------

    def capabilities(self) -> StorageCapabilities:
        return StorageCapabilities(
            range_read=True,
            multipart=True,
            presigned_read=False,  # 本地下载一律走应用代理
            versioning=False,
            server_side_copy=True,
        )

    # -- 基础操作 -------------------------------------------------------

    def put(self, target: ObjectTarget, chunks: Iterable[bytes], options: PutOptions) -> StoredObject:
        dest = _safe_join(self._root, target.key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            raise StorageUnavailable("目标对象已存在，写入不覆盖已提交产物")
        self._enforce_quota(options.max_bytes)
        hasher = StreamingSha256()
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
        try:
            with os.fdopen(tmp_fd, "wb") as handle:
                for chunk in chunks:
                    if not chunk:
                        continue
                    hasher.update(chunk)
                    if options.max_bytes is not None and hasher.byte_count > options.max_bytes:
                        raise StorageQuotaExceeded("写入超出大小上限")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            digest = hasher.hexdigest()
            if options.expected_sha256 and digest != options.expected_sha256:
                raise StorageChecksumMismatch("写入校验失败")
            # 原子发布：同目录 rename，防并发覆盖
            os.link(tmp_path, dest)
            os.unlink(tmp_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
        return StoredObject(
            locator=ObjectLocator(
                profile_revision_id=target.profile_revision_id,
                bucket=None,
                root_ref=target.root_ref,
                key=target.key,
            ),
            size=hasher.byte_count,
            sha256=hasher.hexdigest(),
            provider_etag=None,
        )

    def stat(self, locator: ObjectLocator) -> ObjectStat:
        path = _safe_join(self._root, locator.key)
        if not path.is_file():
            return ObjectStat(exists=False)
        stat = path.stat()
        return ObjectStat(
            exists=True,
            size=stat.st_size,
            provider_etag=None,
            object_version=None,
            content_type=None,
        )

    def open_reader(self, locator: ObjectLocator, byte_range: ByteRange | None) -> BinaryReader:
        path = _safe_join(self._root, locator.key)
        if not path.is_file():
            raise StorageNotFound("对象不存在")
        handle = open(path, "rb")  # noqa: SIM115 - 由 BinaryReader 负责关闭
        if byte_range is not None:
            size = path.stat().st_size
            if byte_range.start >= size or byte_range.end_inclusive >= size:
                handle.close()
                raise StorageUnavailable("Range 越界")
            handle.seek(byte_range.start)
            remaining = byte_range.length
            return _RangeReader(handle, remaining)
        return StreamingReader(handle)

    def delete(self, locator: ObjectLocator) -> DeleteResult:
        path = _safe_join(self._root, locator.key)
        try:
            path.unlink()
            return DeleteResult(deleted=True)
        except FileNotFoundError:
            return DeleteResult(deleted=False, already_absent=True)

    # -- multipart（持久分片） ------------------------------------------

    def initiate(self, target: ObjectTarget) -> MultipartHandle:
        if self.stat(
            ObjectLocator(
                profile_revision_id=target.profile_revision_id,
                bucket=None,
                root_ref=target.root_ref,
                key=target.key,
            )
        ).exists:
            raise StorageUnavailable("目标对象已存在")
        upload_id = uuid.uuid4().hex
        parts_dir = self._root / ".multipart" / upload_id
        parts_dir.mkdir(parents=True, exist_ok=True)
        return MultipartHandle(upload_id=upload_id, target=target, part_size_bytes=0)

    def upload_part(self, handle: MultipartHandle, part_no: int, chunks: Iterable[bytes]) -> PartReceipt:
        if part_no < 1:
            raise StorageUnavailable("part_no 为 1 基应用编号")
        parts_dir = self._root / ".multipart" / handle.upload_id
        if not parts_dir.is_dir():
            raise StorageNotFound("分片会话不存在或已过期")
        hasher = StreamingSha256()
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(parts_dir), suffix=".part")
        with os.fdopen(tmp_fd, "wb") as out:
            for chunk in chunks:
                if chunk:
                    hasher.update(chunk)
                    out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        part_path = parts_dir / f"{part_no:06d}"
        if part_path.exists():
            # 重复 put 同一 part：以最后一次为准（幂等重放）
            part_path.unlink()
        os.link(tmp_path, part_path)
        os.unlink(tmp_path)
        return PartReceipt(part_no=part_no, size=hasher.byte_count, etag=None, sha256=hasher.hexdigest())

    def complete(self, handle: MultipartHandle, parts: list[PartReceipt]) -> StoredObject:
        if not parts:
            raise StorageUnavailable("分片清单为空")
        ordered = sorted(parts, key=lambda p: p.part_no)
        if [p.part_no for p in ordered] != list(range(1, len(ordered) + 1)):
            raise StorageUnavailable("分片编号不连续或有遗漏")
        dest = _safe_join(self._root, handle.target.key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            raise StorageUnavailable("目标对象已存在，写入不覆盖已提交产物")
        parts_dir = self._root / ".multipart" / handle.upload_id
        hasher = StreamingSha256()
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(dest.parent), suffix=".merge")
        try:
            with os.fdopen(tmp_fd, "wb") as out:
                for receipt in ordered:
                    part_path = parts_dir / f"{receipt.part_no:06d}"
                    if not part_path.is_file():
                        raise StorageUnavailable(f"分片 {receipt.part_no} 缺失")
                    with open(part_path, "rb") as part_file:
                        while True:
                            chunk = part_file.read(_CHUNK_SIZE)
                            if not chunk:
                                break
                            hasher.update(chunk)
                            out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            os.link(tmp_path, dest)
            os.unlink(tmp_path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        shutil.rmtree(parts_dir, ignore_errors=True)
        return StoredObject(
            locator=ObjectLocator(
                profile_revision_id=handle.target.profile_revision_id,
                bucket=None,
                root_ref=handle.target.root_ref,
                key=handle.target.key,
            ),
            size=hasher.byte_count,
            sha256=hasher.hexdigest(),
            provider_etag=None,
        )

    def abort(self, handle: MultipartHandle) -> None:
        parts_dir = self._root / ".multipart" / handle.upload_id
        shutil.rmtree(parts_dir, ignore_errors=True)

    # -- 内部 -----------------------------------------------------------

    def _enforce_quota(self, incoming: int | None) -> None:
        if self._quota_bytes is None:
            return
        used = sum(f.stat().st_size for f in self._root.rglob("*") if f.is_file())
        projected = used + (incoming or 0)
        if projected > self._quota_bytes:
            raise StorageQuotaExceeded("本地存储配额不足")

    def compute_sha256(self, locator: ObjectLocator) -> str:
        path = _safe_join(self._root, locator.key)
        if not path.is_file():
            raise StorageNotFound("对象不存在")
        hasher = StreamingSha256()
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_CHUNK_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()


class _RangeReader(StreamingReader):
    def __init__(self, fileobj: object, remaining: int) -> None:
        super().__init__(fileobj)  # type: ignore[arg-type]
        self._remaining = remaining

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        if size < 0 or size > self._remaining:
            size = self._remaining
        data = super().read(size)
        self._remaining -= len(data)
        return data


def verify_content_hash(path: Path, expected_sha256: str) -> bool:
    """独立校验工具（上传完成/迁移校验共用）。"""
    hasher = StreamingSha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest() == expected_sha256
