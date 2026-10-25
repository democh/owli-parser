"""ingestion 应用服务：上传会话与 URL/对象导入。

- 上传：init -> part -> complete；完成只产 artifact，不解析。
- 导入：登记任务独立于解析任务；worker 下载（租约 + fence）→ READY；
  提交解析由 workflow 完成（本模块不直接依赖 jobs）。
- 下载与解析分离：安全 downloader 可访问获准网络，parser 默认无网络。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from owli_parser.adapters.source_secrets import SourceSecretStore
from owli_parser.bootstrap.settings import AppSettings
from owli_parser.core.clock import Clock, ensure_utc
from owli_parser.core.context import ActorKind
from owli_parser.core.crypto import StreamingSha256
from owli_parser.core.errors import (
    ConflictError,
    ForbiddenError,
    InvalidInputError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationFailedError,
)
from owli_parser.core.pagination import decode_cursor, encode_cursor
from owli_parser.infrastructure.telemetry import METRICS
from owli_parser.infrastructure.uow import UnitOfWork
from owli_parser.modules.artifacts.models import UploadRow
from owli_parser.modules.artifacts.service import ArtifactService, ProfileBinding
from owli_parser.modules.catalog.service import CatalogScope, CatalogService
from owli_parser.modules.ingestion import preflight
from owli_parser.modules.ingestion.models import SourceImportRow, SourceLocationRow
from owli_parser.ports.audit import AuditRecorder
from owli_parser.ports.fetcher import (
    FetchBlocked,
    FetchError,
    FetchPolicy,
    FetchResult,
    SafeFetcher,
)
from owli_parser.ports.secrets import SecretUnavailable

_TMP_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class UploadInfo:
    id: uuid.UUID
    dataset_id: uuid.UUID
    filename: str
    state: str
    part_size_bytes: int
    declared_total_bytes: int | None
    received_bytes: int
    parts: list[int]
    artifact_id: uuid.UUID | None
    expires_at: Any


@dataclass(frozen=True, slots=True)
class ImportInfo:
    id: uuid.UUID
    tenant_id: uuid.UUID
    dataset_id: uuid.UUID
    source_kind: str
    display_host: str
    display_path: str
    filename_hint: str | None
    state: str
    attempt: int
    bytes_received: int
    total_bytes: int | None
    artifact_id: uuid.UUID | None
    parsing_job_id: uuid.UUID | None
    error: dict[str, Any] | None


class UploadService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        settings: AppSettings,
        catalog: CatalogService,
        artifacts: ArtifactService,
        audit: AuditRecorder,
        temp_root: Path,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._settings = settings
        self._catalog = catalog
        self._artifacts = artifacts
        self._audit = audit
        self._temp_root = temp_root

    def _upload_dir(self, upload_id: uuid.UUID) -> Path:
        return self._temp_root / "uploads" / str(upload_id)

    async def init_upload(
        self,
        scope: CatalogScope,
        *,
        dataset_id: uuid.UUID,
        filename: str,
        declared_total_bytes: int | None,
    ) -> UploadInfo:
        await self._catalog.require_dataset_write(scope, dataset_id)
        if declared_total_bytes is not None:
            if declared_total_bytes <= 0:
                raise ValidationFailedError("文件大小必须为正")
            if declared_total_bytes > self._settings.upload.total_max_bytes:
                raise PayloadTooLargeError("文件超过允许的最大大小")
        clean_name = preflight.sanitize_display_filename(filename)
        now = self._clock.now()
        actor_id = scope.actor_id or (scope.user_ctx.user_id if scope.user_ctx else None)
        if actor_id is None:
            raise ForbiddenError("缺少执行主体")
        async with self._uow.begin() as session:
            row = UploadRow(
                tenant_id=scope.tenant_id,
                actor_kind=str(scope.kind),
                actor_id=actor_id,
                dataset_id=dataset_id,
                profile_revision_id=None,
                filename=clean_name,
                declared_total_bytes=declared_total_bytes,
                part_size_bytes=self._settings.upload.part_size_bytes,
                parts={},
                state="initiated",
                expires_at=now + timedelta(seconds=self._settings.upload.session_ttl_seconds),
            )
            session.add(row)
            await session.flush()
            self._upload_dir(row.id).mkdir(parents=True, exist_ok=True)
            return self._info(row)

    async def put_part(
        self,
        scope: CatalogScope,
        upload_id: uuid.UUID,
        part_no: int,
        chunks: Iterable[bytes],
    ) -> dict[str, Any]:
        if part_no < 1 or part_no > self._settings.upload.part_max_count:
            raise InvalidInputError("part_no 超出允许范围（1 基）")
        now = self._clock.now()
        async with self._uow.begin() as session:
            row = await self._get_upload_row(session, scope, upload_id)
            if row.state != "initiated":
                raise ConflictError("上传会话状态不允许继续写入")
            if ensure_utc(row.expires_at) <= now:
                row.state = "expired"
                raise ConflictError("上传会话已过期")
            upload_dir = self._upload_dir(row.id)
            declared_total = row.declared_total_bytes
        upload_dir.mkdir(parents=True, exist_ok=True)
        # 文件 IO 放线程池，避免阻塞事件循环；重放同一 part 以最后一次为准
        size, part_sha = await asyncio.to_thread(
            _write_part_file, upload_dir, part_no, chunks, declared_total
        )
        async with self._uow.begin() as session:
            row = await self._get_upload_row(session, scope, upload_id)
            if row.state != "initiated":
                raise ConflictError("上传会话状态不允许继续写入")
            parts: dict[str, Any] = dict(row.parts or {})
            parts[str(part_no)] = {"size": size, "sha256": part_sha}
            row.parts = parts
            await self._audit.record(
                action="ingestion.upload_part",
                actor_kind=ActorKind(scope.kind),
                actor_id=scope.actor_id or (scope.user_ctx.user_id if scope.user_ctx else None),
                tenant_id=scope.tenant_id,
                credential_id=None,
                resource_type="upload",
                resource_id=str(upload_id),
                trace_id="upload",
                decision="allow",
                session=session,
            )
            return {"part_no": part_no, "size": size}

    async def complete_upload(self, scope: CatalogScope, upload_id: uuid.UUID) -> uuid.UUID:
        """校验分片清单与总大小 -> 合并 -> 服务端 SHA-256 -> 生成 artifact。"""
        now = self._clock.now()
        async with self._uow.begin() as session:
            row = await self._get_upload_row(session, scope, upload_id)
            if row.state == "completed" and row.artifact_id is not None:
                return row.artifact_id
            if row.state != "initiated":
                raise ConflictError("上传会话状态不允许完成")
            if ensure_utc(row.expires_at) <= now:
                raise ConflictError("上传会话已过期")
            parts: dict[str, Any] = dict(row.parts or {})
            if not parts:
                raise ValidationFailedError("没有已上传的分片")
            part_numbers = sorted(int(k) for k in parts)
            if part_numbers != list(range(1, len(part_numbers) + 1)):
                raise ValidationFailedError("分片编号不连续，缺少中间分片")
            started_at = ensure_utc(row.created_at)
            declared_total = row.declared_total_bytes
            dataset_id = row.dataset_id
            filename = row.filename
            upload_dir = self._upload_dir(row.id)

        # 事务外（线程池）：合并 + 计算哈希 + 读取头部做 MIME 预检
        merged_path = upload_dir / "merged.bin"
        size, digest = await asyncio.to_thread(_merge_parts, upload_dir, part_numbers)
        if size > self._settings.upload.total_max_bytes:
            await asyncio.to_thread(Path.unlink, merged_path, True)
            raise PayloadTooLargeError("文件超过允许的最大大小")
        if declared_total is not None and size != declared_total:
            await asyncio.to_thread(Path.unlink, merged_path, True)
            raise ValidationFailedError("分片总大小与声明不一致")

        head = await asyncio.to_thread(_read_head, merged_path)
        mime = preflight.detect_mime(head, filename)

        profile = await self._artifact_profile(scope.tenant_id, dataset_id)
        artifact = await self._artifacts.store_object(
            scope.tenant_id,
            kind="source",
            mime=mime,
            content_hash=digest,
            size_bytes=size,
            profile=profile,
            chunks_factory=lambda: _file_chunks(merged_path),
            content_type=mime,
        )

        async with self._uow.begin() as session:
            row = (await session.execute(select(UploadRow).where(UploadRow.id == upload_id))).scalar_one()
            row.state = "completed"
            row.artifact_id = artifact.id
            await self._audit.record(
                action="ingestion.upload_completed",
                actor_kind=ActorKind(scope.kind),
                actor_id=scope.actor_id or (scope.user_ctx.user_id if scope.user_ctx else None),
                tenant_id=scope.tenant_id,
                credential_id=None,
                resource_type="artifact",
                resource_id=str(artifact.id),
                trace_id="upload",
                decision="allow",
                detail={"size": size, "sha256": digest},
                session=session,
            )
        shutil.rmtree(upload_dir, ignore_errors=True)
        METRICS.observe(
            "owli_stage_duration_seconds",
            (self._clock.now() - started_at).total_seconds(),
            {"stage": "upload"},
        )
        return artifact.id

    async def direct_upload(
        self,
        scope: CatalogScope,
        *,
        dataset_id: uuid.UUID,
        filename: str,
        source_file: Any,
    ) -> uuid.UUID:
        """multipart 直传快捷入口：与分片上传同一落盘/校验/artifact 流程，

        边界读取 upload.direct_max_bytes；完成后仍由同一 SubmitParsingCommand 提交解析。
        """
        await self._catalog.require_dataset_write(scope, dataset_id)
        started_at = self._clock.now()
        clean_name = preflight.sanitize_display_filename(filename)
        temp_dir = self._temp_root / "direct" / uuid.uuid4().hex
        temp_dir.mkdir(parents=True, exist_ok=True)
        dest_path = temp_dir / "upload.bin"
        try:
            size, digest, head = await asyncio.to_thread(
                _spool_upload_stream,
                source_file,
                dest_path,
                self._settings.upload.direct_max_bytes,
            )
            mime = preflight.detect_mime(head, clean_name)
            profile = await self._artifact_profile(scope.tenant_id, dataset_id)
            artifact = await self._artifacts.store_object(
                scope.tenant_id,
                kind="source",
                mime=mime,
                content_hash=digest,
                size_bytes=size,
                profile=profile,
                chunks_factory=lambda: _file_chunks(dest_path),
                content_type=mime,
            )
            await self._audit.record(
                action="ingestion.direct_upload",
                actor_kind=ActorKind(scope.kind),
                actor_id=scope.actor_id,
                tenant_id=scope.tenant_id,
                credential_id=None,
                resource_type="artifact",
                resource_id=str(artifact.id),
                trace_id="direct-upload",
                decision="allow",
                detail={"size": size, "sha256": digest, "filename": clean_name},
            )
            METRICS.observe(
                "owli_stage_duration_seconds",
                (self._clock.now() - started_at).total_seconds(),
                {"stage": "upload"},
            )
            return artifact.id
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def cancel_upload(self, scope: CatalogScope, upload_id: uuid.UUID) -> None:
        async with self._uow.begin() as session:
            row = await self._get_upload_row(session, scope, upload_id)
            if row.state == "completed":
                raise ConflictError("上传已完成，不能取消")
            row.state = "cancelled"
        shutil.rmtree(self._upload_dir(upload_id), ignore_errors=True)

    async def get_upload(self, scope: CatalogScope, upload_id: uuid.UUID) -> UploadInfo:
        async with self._uow.begin() as session:
            row = await self._get_upload_row(session, scope, upload_id)
            return self._info(row)

    async def list_uploads(self, scope: CatalogScope) -> list[UploadInfo]:
        async with self._uow.begin() as session:
            rows = (
                await session.execute(
                    select(UploadRow)
                    .where(
                        UploadRow.tenant_id == scope.tenant_id,
                        UploadRow.actor_kind == str(scope.kind),
                    )
                    .order_by(UploadRow.created_at.desc())
                    .limit(100)
                )
            ).scalars()
            return [self._info(r) for r in rows]

    async def _artifact_profile(self, tenant_id: uuid.UUID, dataset_id: uuid.UUID) -> ProfileBinding:
        from owli_parser.modules.catalog.models import DatasetRow

        async with self._uow.read_only() as session:
            dataset = (
                await session.execute(select(DatasetRow).where(DatasetRow.id == dataset_id))
            ).scalar_one()
        return await self._artifacts._router.resolve_for_dataset(tenant_id, dataset)

    async def _get_upload_row(self, session: Any, scope: CatalogScope, upload_id: uuid.UUID) -> UploadRow:
        row = (
            await session.execute(
                select(UploadRow).where(UploadRow.id == upload_id, UploadRow.tenant_id == scope.tenant_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("上传会话不存在")
        if scope.actor_id is not None and row.actor_id != scope.actor_id:
            raise ForbiddenError("不能操作其他主体的上传会话")
        if scope.user_ctx is not None and row.actor_id != scope.user_ctx.user_id:
            raise ForbiddenError("不能操作其他主体的上传会话")
        return cast(UploadRow, row)

    @staticmethod
    def _info(row: UploadRow) -> UploadInfo:
        parts_map: dict[str, Any] = row.parts or {}
        parts = sorted(int(k) for k in parts_map)
        received = sum(int(v["size"]) for v in parts_map.values())
        return UploadInfo(
            id=row.id,
            dataset_id=row.dataset_id,
            filename=row.filename,
            state=row.state,
            part_size_bytes=row.part_size_bytes,
            declared_total_bytes=row.declared_total_bytes,
            received_bytes=received,
            parts=parts,
            artifact_id=row.artifact_id,
            expires_at=row.expires_at,
        )


def _file_chunks(path: Path) -> Iterator[bytes]:
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_TMP_CHUNK)
            if not chunk:
                break
            yield chunk


def _write_part_file(
    upload_dir: Path,
    part_no: int,
    chunks: Iterable[bytes],
    declared_total: int | None,
) -> tuple[int, str]:
    """线程池执行的同步分片写入；返回 (size, sha256)。"""
    part_path = upload_dir / f"{part_no:06d}.part"
    tmp_path = part_path.with_suffix(".tmp")
    hasher = StreamingSha256()
    with open(tmp_path, "wb") as out:
        for chunk in chunks:
            if not chunk:
                continue
            hasher.update(chunk)
            if declared_total is not None and hasher.byte_count > declared_total:
                out.close()
                tmp_path.unlink(missing_ok=True)
                raise PayloadTooLargeError("分片超出声明大小")
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp_path, part_path)  # 重放同一 part 以最后一次为准
    return hasher.byte_count, hasher.hexdigest()


def _merge_parts(upload_dir: Path, part_numbers: list[int]) -> tuple[int, str]:
    """线程池执行的同步合并；返回 (size, sha256)。"""
    hasher = StreamingSha256()
    merged_path = upload_dir / "merged.bin"
    with open(merged_path, "wb") as out:
        for n in part_numbers:
            part_path = upload_dir / f"{n:06d}.part"
            if not part_path.is_file():
                raise ConflictError(f"分片 {n} 缺失")
            with open(part_path, "rb") as part:
                while True:
                    chunk = part.read(_TMP_CHUNK)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    return hasher.byte_count, hasher.hexdigest()


def _read_head(path: Path, size: int = 8192) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(size)


def _spool_upload_stream(
    source_file: Any,
    dest_path: Path,
    max_bytes: int,
) -> tuple[int, str, bytes]:
    """线程池执行：把同步 file-like 流式落到临时文件（有界），返回 (size, sha256, head)。

    直传边界使用 direct_max_bytes；超限即中止并清理，不做全内存缓冲。
    """
    from owli_parser.core.errors import PayloadTooLargeError

    hasher = StreamingSha256()
    head = b""
    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = source_file.read(_TMP_CHUNK)
                if not chunk:
                    break
                if len(head) < 8192:
                    head = (head + chunk)[:8192]
                hasher.update(chunk)
                if hasher.byte_count > max_bytes:
                    out.close()
                    dest_path.unlink(missing_ok=True)
                    raise PayloadTooLargeError("文件超过直传大小上限（请使用分片上传）")
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except Exception:
        dest_path.unlink(missing_ok=True)
        raise
    return hasher.byte_count, hasher.hexdigest(), head


class ImportService:
    """URL / 存储对象导入（下载与解析分离）。"""

    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        settings: AppSettings,
        catalog: CatalogService,
        artifacts: ArtifactService,
        fetcher: SafeFetcher,
        source_secrets: SourceSecretStore,
        audit: AuditRecorder,
        temp_root: Path,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._settings = settings
        self._catalog = catalog
        self._artifacts = artifacts
        self._fetcher = fetcher
        self._source_secrets = source_secrets
        self._audit = audit
        self._temp_root = temp_root

    def _import_dir(self, import_id: uuid.UUID) -> Path:
        return self._temp_root / "imports" / str(import_id)

    def build_fetch_policy(self) -> FetchPolicy:
        s = self._settings
        return FetchPolicy(
            allowed_schemes=tuple(s.imports.allowed_schemes),
            allowed_ports=tuple(s.imports.allowed_ports),
            max_redirects=s.imports.max_redirects,
            connect_timeout_seconds=s.imports.connect_timeout_seconds,
            read_timeout_seconds=s.imports.read_timeout_seconds,
            total_timeout_seconds=s.imports.total_timeout_seconds,
            stalled_timeout_seconds=s.imports.stalled_timeout_seconds,
            max_bytes=s.imports.max_download_bytes,
            allow_private_networks=s.network.allow_private_networks,
            internal_allowlist=tuple(s.network.internal_allowlist),
            retryable_status_codes=tuple(s.imports.retryable_status_codes),
            proxy_url=s.network.proxy_url,
            tls_ca_bundle=s.network.tls_ca_bundle,
            network_mode=s.network.mode,
        )

    # ------------------------------------------------------------------
    # 创建与查询
    # ------------------------------------------------------------------

    async def create_url_import(
        self,
        scope: CatalogScope,
        *,
        dataset_id: uuid.UUID,
        url: str,
        filename_hint: str | None,
        pipeline_ref: str | None,
        document_id: uuid.UUID | None,
        expected_revision: int | None,
    ) -> ImportInfo:
        from urllib.parse import urlparse

        await self._catalog.require_dataset_write(scope, dataset_id)
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ("http", "https"):
            raise InvalidInputError("只允许 HTTP(S) URL")
        if parsed.username or parsed.password:
            raise InvalidInputError("URL 不允许包含凭据")
        host = (parsed.hostname or "").lower()
        if not host:
            raise InvalidInputError("URL 缺少主机名")
        display_path = preflight.sanitize_display_filename(parsed.path or "/", default="/")
        import_id = uuid.uuid4()
        try:
            secret_ref = self._source_secrets.store(scope.tenant_id, import_id, url)
        except SecretUnavailable as exc:
            raise ConflictError("来源加密密钥未配置，无法安全保存 URL") from exc
        actor_id = scope.actor_id or (scope.user_ctx.user_id if scope.user_ctx else None)
        if actor_id is None:
            raise ForbiddenError("缺少执行主体")
        async with self._uow.begin() as session:
            row = SourceImportRow(
                id=import_id,
                tenant_id=scope.tenant_id,
                actor_kind=str(scope.kind),
                actor_id=actor_id,
                credential_id=None,
                dataset_id=dataset_id,
                source_kind="url",
                source_secret_ref=secret_ref,
                source_display_host=host[:255],
                source_display_path=display_path,
                filename_hint=preflight.sanitize_display_filename(filename_hint) if filename_hint else None,
                pipeline_ref=pipeline_ref,
                document_id=document_id,
                expected_revision=expected_revision,
                state="PENDING",
                execution_snapshot={
                    "network_mode": self._settings.network.mode,
                    "max_bytes": self._settings.imports.max_download_bytes,
                },
            )
            session.add(row)
            await session.flush()
            return self._info(row)

    async def create_storage_object_import(
        self,
        scope: CatalogScope,
        *,
        dataset_id: uuid.UUID,
        source_location_id: uuid.UUID,
        relative_key: str,
        object_version: str | None,
        pipeline_ref: str | None,
        document_id: uuid.UUID | None,
        expected_revision: int | None,
    ) -> ImportInfo:
        await self._catalog.require_dataset_write(scope, dataset_id)
        clean_key = relative_key.strip().lstrip("/")
        if not clean_key or ".." in clean_key.split("/") or "\\" in clean_key:
            raise InvalidInputError("relative_key 必须是规范化相对路径")
        async with self._uow.begin() as session:
            location = (
                await session.execute(
                    select(SourceLocationRow).where(
                        SourceLocationRow.id == source_location_id,
                        SourceLocationRow.tenant_id == scope.tenant_id,
                        SourceLocationRow.dataset_id == dataset_id,
                        SourceLocationRow.enabled.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if location is None:
                raise NotFoundError("导入位置不存在或未启用")
            inbox_prefix = location.inbox_prefix.rstrip("/")
            if not (clean_key == inbox_prefix or clean_key.startswith(inbox_prefix + "/")):
                raise ForbiddenError("relative_key 不在该导入位置批准的 inbox 前缀内")
            actor_id = scope.actor_id or (scope.user_ctx.user_id if scope.user_ctx else None)
            if actor_id is None:
                raise ForbiddenError("缺少执行主体")
            row = SourceImportRow(
                tenant_id=scope.tenant_id,
                actor_kind=str(scope.kind),
                actor_id=actor_id,
                dataset_id=dataset_id,
                source_kind="storage_object",
                source_secret_ref=None,
                source_display_host=location.bucket,
                source_display_path=clean_key,
                source_location_id=location.id,
                relative_key=clean_key,
                object_version=object_version,
                filename_hint=preflight.sanitize_display_filename(clean_key.rsplit("/", 1)[-1]),
                pipeline_ref=pipeline_ref,
                document_id=document_id,
                expected_revision=expected_revision,
                state="PENDING",
                execution_snapshot={},
            )
            session.add(row)
            await session.flush()
            return self._info(row)

    async def get_import(self, scope: CatalogScope, import_id: uuid.UUID) -> ImportInfo:
        async with self._uow.begin() as session:
            row = await self._get_import_row(session, scope, import_id)
            return self._info(row)

    async def list_imports(
        self, scope: CatalogScope, *, cursor: str | None, limit: int
    ) -> tuple[list[ImportInfo], str | None, bool]:
        conditions = [SourceImportRow.tenant_id == scope.tenant_id]
        if cursor:
            data = decode_cursor(cursor)
            created = data.get("ts")
            last_id = data.get("id")
            if created and last_id:
                from datetime import datetime as dt

                conditions.append(SourceImportRow.created_at < dt.fromisoformat(str(created)))
        async with self._uow.begin() as session:
            rows = (
                (
                    await session.execute(
                        select(SourceImportRow)
                        .where(*conditions)
                        .order_by(SourceImportRow.created_at.desc(), SourceImportRow.id)
                        .limit(limit + 1)
                    )
                )
                .scalars()
                .all()
            )
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            encode_cursor({"ts": items[-1].created_at.isoformat(), "id": str(items[-1].id)})
            if has_more and items
            else None
        )
        return [self._info(r) for r in items], next_cursor, has_more

    async def cancel_import(self, scope: CatalogScope, import_id: uuid.UUID) -> ImportInfo:
        async with self._uow.begin() as session:
            row = await self._get_import_row(session, scope, import_id)
            if row.state == "SUBMITTED":
                raise ConflictError("导入已提交解析，请改用任务取消接口")
            if row.state in ("CANCELLED", "FAILED"):
                return self._info(row)
            row.state = "CANCELLED"
            if row.source_secret_ref:
                with contextlib.suppress(SecretUnavailable):
                    self._source_secrets.delete(row.source_secret_ref)
                row.source_secret_ref = None
            info = self._info(row)
        shutil.rmtree(self._import_dir(import_id), ignore_errors=True)
        return info

    async def retry_import(
        self, scope: CatalogScope, import_id: uuid.UUID, *, max_attempts: int
    ) -> ImportInfo:
        async with self._uow.begin() as session:
            row = await self._get_import_row(session, scope, import_id)
            if row.state not in ("FAILED", "RETRY_WAIT"):
                raise ConflictError("仅失败或待重试的导入可以重试")
            if row.attempt >= max_attempts:
                row.state = "FAILED"
                raise ConflictError("重试次数已达上限")
            row.attempt += 1
            row.state = "PENDING"
            row.error = None
            return self._info(row)

    # ------------------------------------------------------------------
    # 下载执行（worker）
    # ------------------------------------------------------------------

    async def claim_for_download(self, import_id: uuid.UUID, owner: str) -> int | None:
        """CAS 获取下载租约；返回 fence，None 表示不可领取。"""
        now = self._clock.now()
        async with self._uow.begin() as session:
            row = (
                await session.execute(select(SourceImportRow).where(SourceImportRow.id == import_id))
            ).scalar_one_or_none()
            if row is None:
                return None
            if row.state not in ("PENDING", "RETRY_WAIT"):
                return None
            if row.lease_until is not None and row.lease_until > now:
                return None
            row.state = "FETCHING"
            row.lease_owner = owner
            row.lease_until = now + timedelta(seconds=self._settings.jobs.lease_seconds)
            row.fence += 1
            return row.fence

    async def release_lease(
        self,
        import_id: uuid.UUID,
        fence: int,
        *,
        state: str,
        error: dict[str, Any] | None = None,
    ) -> None:
        async with self._uow.begin() as session:
            row = (
                await session.execute(select(SourceImportRow).where(SourceImportRow.id == import_id))
            ).scalar_one_or_none()
            if row is None or row.fence != fence:
                return  # 旧 fence 不得覆盖状态
            row.lease_owner = None
            row.lease_until = None
            row.state = state
            row.error = error

    def download_to_temp(self, import_id: uuid.UUID, url: str) -> FetchResult:
        """同步下载到隔离目录（worker 子进程执行）。"""
        import_dir = self._import_dir(import_id)
        import_dir.mkdir(parents=True, exist_ok=True)
        dest_path = import_dir / "download.bin"
        policy = self.build_fetch_policy()
        result = self._fetcher.fetch(
            url,
            policy=policy,
            dest_factory=lambda: open(dest_path, "wb"),  # noqa: SIM115 - 由 fetcher 关闭
        )
        return result

    async def register_downloaded_artifact(
        self,
        import_id: uuid.UUID,
        fence: int,
        *,
        content_hash: str,
        size_bytes: int,
        content_type_hint: str | None,
    ) -> uuid.UUID | None:
        """把下载临时文件写入存储并登记 artifact；返回 artifact_id。"""
        async with self._uow.begin() as session:
            row = (
                await session.execute(select(SourceImportRow).where(SourceImportRow.id == import_id))
            ).scalar_one_or_none()
            if row is None or row.fence != fence:
                return None
            tenant_id = row.tenant_id
            dataset_id = row.dataset_id
            filename_hint = row.filename_hint
        import_dir = self._import_dir(import_id)
        dest_path = import_dir / "download.bin"
        if not dest_path.is_file():
            raise NotFoundError("下载临时文件不存在")
        head = await asyncio.to_thread(_read_head, dest_path)
        mime = preflight.detect_mime(head, filename_hint) or content_type_hint
        from owli_parser.modules.catalog.models import DatasetRow

        async with self._uow.read_only() as session:
            dataset = (
                await session.execute(select(DatasetRow).where(DatasetRow.id == dataset_id))
            ).scalar_one()
        profile = await self._artifacts._router.resolve_for_dataset(tenant_id, dataset)
        artifact = await self._artifacts.store_object(
            tenant_id,
            kind="source",
            mime=mime,
            content_hash=content_hash,
            size_bytes=size_bytes,
            profile=profile,
            chunks_factory=lambda: _file_chunks(dest_path),
            content_type=mime,
        )
        return artifact.id

    async def mark_ready(self, import_id: uuid.UUID, fence: int, artifact_id: uuid.UUID) -> None:
        async with self._uow.begin() as session:
            row = (
                await session.execute(select(SourceImportRow).where(SourceImportRow.id == import_id))
            ).scalar_one_or_none()
            if row is None or row.fence != fence:
                return
            row.state = "READY"
            row.artifact_id = artifact_id
            row.lease_owner = None
            row.lease_until = None

    async def mark_submitted(self, import_id: uuid.UUID, parsing_job_id: uuid.UUID) -> None:
        """READY -> SUBMITTED；parsing_job_id 唯一，重复登记被拒绝。"""
        async with self._uow.begin() as session:
            row = (
                await session.execute(select(SourceImportRow).where(SourceImportRow.id == import_id))
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("导入不存在")
            if row.parsing_job_id is not None:
                if row.parsing_job_id != parsing_job_id:
                    raise ConflictError("该导入已登记其他解析任务")
                return
            if row.state not in ("READY", "SUBMITTED"):
                raise ConflictError("导入尚未就绪，不能登记解析任务")
            row.parsing_job_id = parsing_job_id
            row.state = "SUBMITTED"
            try:
                await session.flush()
            except IntegrityError as exc:
                raise ConflictError("该导入已登记其他解析任务") from exc
            # 来源引用按保留策略回收：下载完成后不再需要完整 URL
            if row.source_secret_ref:
                with contextlib.suppress(SecretUnavailable):
                    self._source_secrets.delete(row.source_secret_ref)
                row.source_secret_ref = None

    def load_source_url(self, import_id: uuid.UUID, ref: str) -> str:
        del import_id
        return self._source_secrets.load(ref)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _get_import_row(
        self, session: Any, scope: CatalogScope, import_id: uuid.UUID
    ) -> SourceImportRow:
        row = (
            await session.execute(
                select(SourceImportRow).where(
                    SourceImportRow.id == import_id,
                    SourceImportRow.tenant_id == scope.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("导入任务不存在")
        # 应用只能操作同应用创建的导入（所有权绑定 application_id）
        if (
            scope.kind is ActorKind.APPLICATION
            and scope.actor_id is not None
            and row.actor_id != scope.actor_id
        ):
            raise NotFoundError("导入任务不存在")
        return cast(SourceImportRow, row)

    @staticmethod
    def _info(row: SourceImportRow) -> ImportInfo:
        return ImportInfo(
            id=row.id,
            tenant_id=row.tenant_id,
            dataset_id=row.dataset_id,
            source_kind=row.source_kind,
            display_host=row.source_display_host,
            display_path=row.source_display_path,
            filename_hint=row.filename_hint,
            state=row.state,
            attempt=row.attempt,
            bytes_received=row.bytes_received,
            total_bytes=row.total_bytes,
            artifact_id=row.artifact_id,
            parsing_job_id=row.parsing_job_id,
            error=row.error,
        )


def wrap_fetch_errors(exc: Exception) -> tuple[bool, str, str]:
    """把 fetcher 异常分类为 (retryable, code, message)。"""
    if isinstance(exc, FetchBlocked):
        return False, "ssrf_blocked", str(exc)
    if isinstance(exc, FetchError):
        return exc.retryable, "fetch_failed", str(exc)
    return False, "fetch_failed", "下载失败"


__all__ = [
    "ImportInfo",
    "ImportService",
    "UploadInfo",
    "UploadService",
    "wrap_fetch_errors",
]
