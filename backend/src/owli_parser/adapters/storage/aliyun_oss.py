"""AliyunOssStorage：阿里云 OSS 原生 SDK（alibabacloud-oss-v2）实现。

- 使用原生 SDK（不以 S3 兼容替代），支持 OSS 分片、Range、签名与错误映射。
- 状态：SDK 调用路径未经真实 OSS 环境验收前标记为“集成未验证”；
  capabilities 不因代码存在即宣称云端已验证。
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from datetime import timedelta
from typing import Any, cast

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
    SignedRead,
    SignedReadOptions,
    StorageCapabilities,
    StorageChecksumMismatch,
    StorageNotFound,
    StoragePermissionDenied,
    StorageUnavailable,
    StoredObject,
)


def _spool_chunks(chunks: Iterable[bytes], options: PutOptions) -> tuple[str, int, str]:
    hasher = StreamingSha256()
    fd, tmp_path = tempfile.mkstemp(suffix=".ossput")
    with os.fdopen(fd, "wb") as handle:
        for chunk in chunks:
            if not chunk:
                continue
            hasher.update(chunk)
            if options.max_bytes is not None and hasher.byte_count > options.max_bytes:
                handle.close()
                os.unlink(tmp_path)
                raise StorageUnavailable("写入超出大小上限")
            handle.write(chunk)
        handle.flush()
        os.fsync(handle.fileno())
    return tmp_path, hasher.byte_count, hasher.hexdigest()


class AliyunOssStorageBackend:
    provider = "aliyun_oss"

    def __init__(
        self,
        *,
        region: str,
        endpoint: str,
        bucket: str,
        access_key_id: str,
        access_key_secret: str,
        use_internal_endpoint: bool = False,
        client: Any | None = None,
    ) -> None:
        """client 可注入（contract suite 使用 fake）；None 时按配置创建真实 OSS client。"""
        import alibabacloud_oss_v2 as oss

        self._oss = oss
        self._bucket = bucket
        if client is not None:
            self._client = client
            return
        try:
            credentials = oss.credentials.StaticCredentialsProvider(
                access_key_id=access_key_id, access_key_secret=access_key_secret
            )
        except AttributeError as exc:  # pragma: no cover - 依赖缺失/版本不符路径
            raise StorageUnavailable("storage-oss extra 未安装（uv sync --extra storage-oss）") from exc
        cfg = oss.config.load_default()
        cfg.credentials_provider = credentials
        cfg.region = region
        cfg.endpoint = endpoint
        cfg.use_internal_endpoint = use_internal_endpoint
        self._client = oss.Client(cfg)

    def capabilities(self) -> StorageCapabilities:
        return StorageCapabilities(
            range_read=True,
            multipart=True,
            presigned_read=True,
            versioning=False,
            server_side_copy=True,
        )

    # -- 基础操作 -------------------------------------------------------

    def put(self, target: ObjectTarget, chunks: Iterable[bytes], options: PutOptions) -> StoredObject:
        oss = self._oss
        bucket = target.bucket or self._bucket
        tmp_path, size, digest = _spool_chunks(chunks, options)
        try:
            if options.expected_sha256 and digest != options.expected_sha256:
                raise StorageChecksumMismatch("写入校验失败")
            with open(tmp_path, "rb") as body:
                result = self._client.put_object(
                    oss.PutObjectRequest(
                        bucket=bucket,
                        key=target.key,
                        body=body,
                        content_type=options.content_type,
                    )
                )
        except StorageChecksumMismatch:
            raise
        except Exception as exc:
            raise self._map_error(exc) from exc
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        return StoredObject(
            locator=ObjectLocator(
                profile_revision_id=target.profile_revision_id,
                bucket=bucket,
                root_ref=None,
                key=target.key,
            ),
            size=size,
            sha256=digest or None,
            provider_etag=getattr(result, "etag", None),
        )

    def stat(self, locator: ObjectLocator) -> ObjectStat:
        oss = self._oss
        bucket = locator.bucket or self._bucket
        try:
            result = self._client.head_object(oss.HeadObjectRequest(bucket=bucket, key=locator.key))
        except Exception as exc:
            mapped = self._map_error(exc)
            if isinstance(mapped, StorageNotFound):
                return ObjectStat(exists=False)
            raise mapped from exc
        return ObjectStat(
            exists=True,
            size=int(getattr(result, "content_length", 0) or 0),
            provider_etag=getattr(result, "etag", None),
            object_version=getattr(result, "version_id", None),
            content_type=getattr(result, "content_type", None),
        )

    def open_reader(self, locator: ObjectLocator, byte_range: ByteRange | None) -> BinaryReader:
        oss = self._oss
        bucket = locator.bucket or self._bucket
        request = oss.GetObjectRequest(bucket=bucket, key=locator.key)
        if byte_range is not None:
            request.range = f"bytes={byte_range.start}-{byte_range.end_inclusive}"
        try:
            response = self._client.get_object(request)

            class _OssBody(BinaryReader):
                def __init__(self, body: Any) -> None:
                    self._body = body

                def read(self, size: int = -1) -> bytes:
                    return cast(bytes, self._body.read(size))

                def close(self) -> None:
                    self._body.close()

                def __enter__(self) -> _OssBody:
                    return self

                def __exit__(self, *exc_info: object) -> None:
                    self.close()

            return _OssBody(response.body)
        except Exception as exc:
            raise self._map_error(exc) from exc

    def delete(self, locator: ObjectLocator) -> DeleteResult:
        oss = self._oss
        bucket = locator.bucket or self._bucket
        try:
            self._client.delete_object(oss.DeleteObjectRequest(bucket=bucket, key=locator.key))
        except Exception as exc:
            raise self._map_error(exc) from exc
        return DeleteResult(deleted=True)

    def presign_read(self, locator: ObjectLocator, options: SignedReadOptions) -> SignedRead:
        import time

        oss = self._oss
        bucket = locator.bucket or self._bucket
        request = oss.GetObjectRequest(bucket=bucket, key=locator.key)
        if options.content_disposition:
            request.response_content_disposition = options.content_disposition
        result = self._client.presign(request, expires=timedelta(seconds=options.ttl_seconds))
        return SignedRead(url=result.url, expires_at_epoch=int(time.time()) + options.ttl_seconds)

    # -- multipart ------------------------------------------------------

    def initiate(self, target: ObjectTarget) -> MultipartHandle:
        oss = self._oss
        bucket = target.bucket or self._bucket
        try:
            result = self._client.initiate_multipart_upload(
                oss.InitiateMultipartUploadRequest(bucket=bucket, key=target.key)
            )
        except Exception as exc:
            raise self._map_error(exc) from exc
        return MultipartHandle(upload_id=result.upload_id, target=target, part_size_bytes=8 * 1024 * 1024)

    def upload_part(self, handle: MultipartHandle, part_no: int, chunks: Iterable[bytes]) -> PartReceipt:
        if part_no < 1:
            raise StorageUnavailable("part_no 为 1 基应用编号")
        oss = self._oss
        bucket = handle.target.bucket or self._bucket
        tmp_path, size, digest = _spool_chunks(chunks, PutOptions())
        try:
            with open(tmp_path, "rb") as body:
                result = self._client.upload_part(
                    oss.UploadPartRequest(
                        bucket=bucket,
                        key=handle.target.key,
                        upload_id=handle.upload_id,
                        part_number=part_no,
                        body=body,
                    )
                )
        except Exception as exc:
            raise self._map_error(exc) from exc
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        return PartReceipt(part_no=part_no, size=size, etag=result.etag, sha256=digest)

    def complete(self, handle: MultipartHandle, parts: list[PartReceipt]) -> StoredObject:
        if not parts:
            raise StorageUnavailable("分片清单为空")
        ordered = sorted(parts, key=lambda p: p.part_no)
        if [p.part_no for p in ordered] != list(range(1, len(ordered) + 1)):
            raise StorageUnavailable("分片编号不连续或有遗漏")
        oss = self._oss
        bucket = handle.target.bucket or self._bucket
        try:
            result = self._client.complete_multipart_upload(
                oss.CompleteMultipartUploadRequest(
                    bucket=bucket,
                    key=handle.target.key,
                    upload_id=handle.upload_id,
                    complete_multipart_upload=oss.CompleteMultipartUpload(
                        parts=[oss.UploadPart(part_number=p.part_no, etag=p.etag) for p in ordered]
                    ),
                )
            )
        except Exception as exc:
            raise self._map_error(exc) from exc
        total = sum(p.size for p in ordered)
        return StoredObject(
            locator=ObjectLocator(
                profile_revision_id=handle.target.profile_revision_id,
                bucket=bucket,
                root_ref=None,
                key=handle.target.key,
            ),
            size=total,
            sha256=None,  # OSS multipart ETag/CRC64 不能当作 SHA-256
            provider_etag=getattr(result, "etag", None),
        )

    def abort(self, handle: MultipartHandle) -> None:
        oss = self._oss
        bucket = handle.target.bucket or self._bucket
        try:
            self._client.abort_multipart_upload(
                oss.AbortMultipartUploadRequest(
                    bucket=bucket, key=handle.target.key, upload_id=handle.upload_id
                )
            )
        except Exception as exc:
            raise self._map_error(exc) from exc

    # -- 错误映射 -------------------------------------------------------

    def _map_error(self, exc: Exception) -> Exception:
        oss = self._oss
        message = str(exc)
        status = getattr(exc, "status_code", None) or getattr(
            getattr(exc, "response", None), "status_code", None
        )
        code = getattr(getattr(exc, "response", None), "code", None) or getattr(exc, "code", None)
        if status == 404 or code in ("NoSuchKey", "NoSuchBucket"):
            return StorageNotFound("OSS 对象不存在")
        if status in (401, 403) or code in (
            "AccessDenied",
            "InvalidAccessKeyId",
            "SignatureDoesNotMatch",
        ):
            return StoragePermissionDenied("OSS 访问被拒绝")
        if isinstance(exc, (oss.exceptions.ServiceError,)):
            return StorageUnavailable(f"OSS 服务错误（{code or status}）")
        return StorageUnavailable(f"OSS 调用失败: {message[:120]}")
