"""S3Storage：基于锁定 boto3/botocore 的 S3 兼容实现。

- AWS S3 与各兼容服务分别验收，兼容协议不保证全部特性相同。
- 凭据来自 SecretResolver（访问键不进镜像/日志）；重试由全局预算控制。
- 未测试过真实云链路时不得向外宣称该集成已验证。
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from typing import Any

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
    StreamingReader,
)

_CHUNK_SIZE = 1024 * 1024


def _spool_chunks(chunks: Iterable[bytes], options: PutOptions) -> tuple[str, int, str]:
    """把输入流落到本地临时文件并流式计算 SHA-256。"""
    hasher = StreamingSha256()
    fd, tmp_path = tempfile.mkstemp(suffix=".s3put")
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


class S3StorageBackend:
    """StorageBackend + MultipartStorage + SignedReadStorage。"""

    provider = "s3"

    def __init__(
        self,
        *,
        endpoint_url: str | None,
        region: str,
        bucket: str,
        addressing_style: str,
        access_key: str,
        secret_key: str,
        verify_tls: bool = True,
        client: Any | None = None,
    ) -> None:
        """client 可注入（contract suite 使用 fake）；None 时按配置创建真实 boto3 client。"""
        if client is not None:
            self._client = client
            self._bucket = bucket
            return
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise StorageUnavailable("storage-s3 extra 未安装（uv sync --extra storage-s3）") from exc
        config = boto3.session.Config(
            s3={"addressing_style": addressing_style},
            retries={"max_attempts": 1, "mode": "standard"},
            connect_timeout=10,
            read_timeout=30,
        )
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            verify=verify_tls,
            config=config,
        )
        self._bucket = bucket

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
        bucket = target.bucket or self._bucket
        tmp_path, size, digest = _spool_chunks(chunks, options)
        try:
            if options.expected_sha256 and digest != options.expected_sha256:
                raise StorageChecksumMismatch("写入校验失败")
            with open(tmp_path, "rb") as handle:
                self._client.upload_fileobj(
                    handle,
                    bucket,
                    target.key,
                    ExtraArgs=({"ContentType": options.content_type} if options.content_type else None),
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
            sha256=digest,
            provider_etag=None,
        )

    def stat(self, locator: ObjectLocator) -> ObjectStat:
        bucket = locator.bucket or self._bucket
        try:
            response = self._client.head_object(Bucket=bucket, Key=locator.key)
        except Exception as exc:
            mapped = self._map_error(exc)
            if isinstance(mapped, StorageNotFound):
                return ObjectStat(exists=False)
            raise mapped from exc
        return ObjectStat(
            exists=True,
            size=int(response.get("ContentLength", 0)),
            provider_etag=(response.get("ETag") or "").strip('"') or None,
            object_version=response.get("VersionId"),
            content_type=response.get("ContentType"),
        )

    def open_reader(self, locator: ObjectLocator, byte_range: ByteRange | None) -> BinaryReader:
        bucket = locator.bucket or self._bucket
        kwargs: dict[str, Any] = {"Bucket": bucket, "Key": locator.key}
        if byte_range is not None:
            kwargs["Range"] = f"bytes={byte_range.start}-{byte_range.end_inclusive}"
        try:
            response = self._client.get_object(**kwargs)
        except Exception as exc:
            raise self._map_error(exc) from exc
        return StreamingReader(response["Body"])

    def delete(self, locator: ObjectLocator) -> DeleteResult:
        bucket = locator.bucket or self._bucket
        try:
            self._client.delete_object(Bucket=bucket, Key=locator.key)
        except Exception as exc:
            raise self._map_error(exc) from exc
        return DeleteResult(deleted=True)

    def presign_read(self, locator: ObjectLocator, options: SignedReadOptions) -> SignedRead:
        import time

        bucket = locator.bucket or self._bucket
        params: dict[str, Any] = {"Bucket": bucket, "Key": locator.key}
        if options.content_disposition:
            params["ResponseContentDisposition"] = options.content_disposition
        url = self._client.generate_presigned_url("get_object", Params=params, ExpiresIn=options.ttl_seconds)
        return SignedRead(url=url, expires_at_epoch=int(time.time()) + options.ttl_seconds)

    # -- multipart ------------------------------------------------------

    def initiate(self, target: ObjectTarget) -> MultipartHandle:
        bucket = target.bucket or self._bucket
        try:
            response = self._client.create_multipart_upload(Bucket=bucket, Key=target.key)
        except Exception as exc:
            raise self._map_error(exc) from exc
        return MultipartHandle(upload_id=response["UploadId"], target=target, part_size_bytes=8 * 1024 * 1024)

    def upload_part(self, handle: MultipartHandle, part_no: int, chunks: Iterable[bytes]) -> PartReceipt:
        if part_no < 1:
            raise StorageUnavailable("part_no 为 1 基应用编号")
        bucket = handle.target.bucket or self._bucket
        tmp_path, size, digest = _spool_chunks(chunks, PutOptions())
        try:
            with open(tmp_path, "rb") as body:
                response = self._client.upload_part(
                    Bucket=bucket,
                    Key=handle.target.key,
                    UploadId=handle.upload_id,
                    PartNumber=part_no,
                    Body=body,
                )
        except Exception as exc:
            raise self._map_error(exc) from exc
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        return PartReceipt(part_no=part_no, size=size, etag=response.get("ETag"), sha256=digest)

    def complete(self, handle: MultipartHandle, parts: list[PartReceipt]) -> StoredObject:
        if not parts:
            raise StorageUnavailable("分片清单为空")
        ordered = sorted(parts, key=lambda p: p.part_no)
        if [p.part_no for p in ordered] != list(range(1, len(ordered) + 1)):
            raise StorageUnavailable("分片编号不连续或有遗漏")
        bucket = handle.target.bucket or self._bucket
        try:
            response = self._client.complete_multipart_upload(
                Bucket=bucket,
                Key=handle.target.key,
                UploadId=handle.upload_id,
                MultipartUpload={"Parts": [{"PartNumber": p.part_no, "ETag": p.etag} for p in ordered]},
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
            sha256=None,  # multipart ETag 不能当作 SHA-256；调用方独立校验
            provider_etag=(response.get("ETag") or "").strip('"') or None,
        )

    def abort(self, handle: MultipartHandle) -> None:
        bucket = handle.target.bucket or self._bucket
        try:
            self._client.abort_multipart_upload(
                Bucket=bucket, Key=handle.target.key, UploadId=handle.upload_id
            )
        except Exception as exc:
            raise self._map_error(exc) from exc

    # -- 错误映射 -------------------------------------------------------

    def _map_error(self, exc: Exception) -> Exception:
        from botocore.exceptions import ClientError, EndpointConnectionError

        if isinstance(exc, EndpointConnectionError):
            return StorageUnavailable("S3 端点不可达")
        if isinstance(exc, ClientError):
            code = str(exc.response.get("Error", {}).get("Code", ""))
            http_status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            if code in ("NoSuchKey", "404", "NoSuchBucket") or http_status == 404:
                return StorageNotFound("S3 对象不存在")
            if code in (
                "AccessDenied",
                "InvalidAccessKeyId",
                "SignatureDoesNotMatch",
            ) or http_status in (401, 403):
                return StoragePermissionDenied("S3 访问被拒绝")
            return StorageUnavailable(f"S3 调用失败（{code or http_status}）")
        return StorageUnavailable("S3 调用失败")
