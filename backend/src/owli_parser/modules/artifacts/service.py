"""ArtifactService 与 StorageRouter。

- ArtifactService 管理逻辑产物/位置与租户授权；业务模块只持 artifact_id，
  不拼接 s3://、OSS URL 或本地路径。
- StorageRouter 按 profile revision 返回 adapter；新写入按数据集绑定的
  storage profile 选址，已存在对象按 location 的原 revision 读取。
- 写入协议：登记 pending location -> 写入对象 -> CAS 标记 active；
  失败或超时的 pending 由孤儿回收处理（先删对象再删行）。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from owli_parser.bootstrap.settings import AppSettings
from owli_parser.core.clock import Clock
from owli_parser.core.errors import ChecksumMismatchError, NotFoundError, StorageUnavailableError
from owli_parser.infrastructure.uow import UnitOfWork
from owli_parser.modules.artifacts.models import ArtifactLocationRow, ArtifactRow
from owli_parser.ports.secrets import SecretResolver, SecretUnavailable
from owli_parser.ports.storage import (
    ByteRange,
    ObjectLocator,
    ObjectTarget,
    PutOptions,
    StorageBackend,
    StorageChecksumMismatch,
    StorageError,
    StorageNotFound,
    StorageQuotaExceeded,
    StorageUnavailable,
    UnsupportedCapability,
)

_LOCAL_PROFILE_CACHE: dict[str, StorageBackend] = {}


@dataclass(frozen=True, slots=True)
class ProfileBinding:
    """已解析的存储 profile：包含实例化 backend 与选址信息。"""

    profile_id: uuid.UUID | None  # None 表示 boot 内置 local profile
    name: str
    provider: str
    backend: StorageBackend
    bucket: str | None
    root_ref: str | None
    write_disabled: bool = False
    read_disabled: bool = False


@dataclass(frozen=True, slots=True)
class ArtifactInfo:
    id: uuid.UUID
    tenant_id: uuid.UUID
    content_hash: str
    size_bytes: int
    mime: str | None
    kind: str
    ref_count: int


@dataclass(frozen=True, slots=True)
class LocationInfo:
    id: uuid.UUID
    artifact_id: uuid.UUID
    provider: str
    bucket: str | None
    root_ref: str | None
    object_key: str
    object_version: str | None
    size_bytes: int
    checksum_sha256: str
    state: str
    profile_revision_id: uuid.UUID | None


class StorageRouter:
    """按 profile 解析存储后端；缓存本地实例，云端实例按配置构建。"""

    def __init__(
        self,
        settings: AppSettings,
        secrets: SecretResolver,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._secrets = secrets
        self._session_factory = session_factory

    # -- 解析 -----------------------------------------------------------

    async def resolve_by_id(self, profile_id: uuid.UUID) -> ProfileBinding:
        from owli_parser.modules.configuration.models import StorageProfileRow

        async with self._session_factory() as session:
            row = (
                await session.execute(select(StorageProfileRow).where(StorageProfileRow.id == profile_id))
            ).scalar_one_or_none()
            if row is None:
                raise StorageUnavailableError("存储 profile 不存在")
            return self._binding_from_row(row)

    async def resolve_default(self, tenant_id: uuid.UUID) -> ProfileBinding:
        """数据集未绑定 profile 时使用：DB 中同名 published 最新版本；
        不存在则回退 boot 内置 local profile。"""
        name = self._settings.storage.default_profile_ref
        from owli_parser.modules.configuration.models import StorageProfileRow

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(StorageProfileRow)
                    .where(
                        StorageProfileRow.tenant_id == tenant_id,
                        StorageProfileRow.name == name,
                        StorageProfileRow.state == "published",
                    )
                    .order_by(StorageProfileRow.revision.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is not None:
                return self._binding_from_row(row)
        return self._boot_local_binding(name)

    async def resolve_for_dataset(self, tenant_id: uuid.UUID, dataset: Any) -> ProfileBinding:
        profile_id = getattr(dataset, "storage_profile_id", None)
        if profile_id is not None:
            return await self.resolve_by_id(profile_id)
        return await self.resolve_default(tenant_id)

    def resolve_boot_local(self) -> ProfileBinding:
        """boot 内置 local profile（DB 未配置同名 profile 时的回退）。"""
        return self._boot_local_binding(self._settings.storage.default_profile_ref)

    # -- 内部 -----------------------------------------------------------

    def _boot_local_binding(self, name: str) -> ProfileBinding:
        roots = self._settings.storage.local.roots
        if not roots:
            raise StorageUnavailableError("未配置本地存储 root，无法写入")
        root = roots[0]
        backend = self._local_backend(root.path, root.quota_bytes)
        return ProfileBinding(
            profile_id=None,
            name=name,
            provider="local",
            backend=backend,
            bucket=None,
            root_ref=root.id,
        )

    def _local_backend(self, path: str, quota_bytes: int | None) -> StorageBackend:
        key = f"{path}|{quota_bytes}"
        cached = _LOCAL_PROFILE_CACHE.get(key)
        if cached is not None:
            return cached
        from owli_parser.adapters.storage.local import LocalStorageBackend

        backend = LocalStorageBackend(Path(path), quota_bytes)
        _LOCAL_PROFILE_CACHE[key] = backend
        return backend

    def _binding_from_row(self, row: Any) -> ProfileBinding:
        from owli_parser.modules.configuration.models import StorageProfileRow

        assert isinstance(row, StorageProfileRow)
        return self.binding_from_config(
            provider=row.provider,
            config=row.config or {},
            profile_id=row.id,
            name=row.name,
            write_disabled=row.write_disabled,
            read_disabled=row.read_disabled,
        )

    def binding_from_config(
        self,
        *,
        provider: str,
        config: dict[str, Any],
        profile_id: uuid.UUID | None = None,
        name: str = "profile",
        write_disabled: bool = False,
        read_disabled: bool = False,
    ) -> ProfileBinding:
        """从（可能是草稿的）配置构造可用的 ProfileBinding。

        供 settings 校验/测试与正式读取共用；local root 必须在平台批准目录池中。
        """
        if provider == "local":
            root_path = str(config.get("root_path", ""))
            root_ref = str(config.get("root_ref", ""))
            if not root_path:
                raise StorageUnavailableError("local profile 缺少 root_path 配置")
            allowed_roots = {r.id: r for r in self._settings.storage.local.roots}
            if root_ref and root_ref not in allowed_roots:
                raise StorageUnavailableError("local root 未在平台批准目录池中")
            backend = self._local_backend(root_path, config.get("quota_bytes"))
            return ProfileBinding(
                profile_id=profile_id,
                name=name,
                provider=provider,
                backend=backend,
                bucket=None,
                root_ref=root_ref or None,
                write_disabled=write_disabled,
                read_disabled=read_disabled,
            )
        if provider == "s3":
            try:
                secret = self._secrets.resolve(str(config.get("credential_ref", "")))
            except SecretUnavailable as exc:
                raise StorageUnavailableError("S3 凭据不可解析") from exc
            access_key, _, secret_key = secret.partition(":")
            from owli_parser.adapters.storage.s3 import S3StorageBackend

            backend = S3StorageBackend(
                endpoint_url=config.get("endpoint"),
                region=str(config.get("region", "us-east-1")),
                bucket=str(config.get("bucket", "")),
                addressing_style=str(config.get("addressing_style", "auto")),
                access_key=access_key,
                secret_key=secret_key,
                verify_tls=bool(config.get("verify_tls", True)),
            )
            return ProfileBinding(
                profile_id=profile_id,
                name=name,
                provider=provider,
                backend=backend,
                bucket=str(config.get("bucket", "")),
                root_ref=None,
                write_disabled=write_disabled,
                read_disabled=read_disabled,
            )
        if provider == "aliyun_oss":
            try:
                secret = self._secrets.resolve(str(config.get("credential_ref", "")))
            except SecretUnavailable as exc:
                raise StorageUnavailableError("OSS 凭据不可解析") from exc
            access_key_id, _, access_key_secret = secret.partition(":")
            from owli_parser.adapters.storage.aliyun_oss import AliyunOssStorageBackend

            backend = AliyunOssStorageBackend(
                region=str(config.get("region", "")),
                endpoint=str(config.get("endpoint", "")),
                bucket=str(config.get("bucket", "")),
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
                use_internal_endpoint=bool(config.get("use_internal_endpoint", False)),
            )
            return ProfileBinding(
                profile_id=profile_id,
                name=name,
                provider=provider,
                backend=backend,
                bucket=str(config.get("bucket", "")),
                root_ref=None,
                write_disabled=write_disabled,
                read_disabled=read_disabled,
            )
        raise StorageUnavailableError(f"未知存储 provider: {provider}")


class ArtifactService:
    def __init__(self, uow: UnitOfWork, clock: Clock, router: StorageRouter) -> None:
        self._uow = uow
        self._clock = clock
        self._router = router

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    @staticmethod
    def build_object_key(tenant_id: uuid.UUID, kind: str, artifact_id: uuid.UUID) -> str:
        """对象键由服务端生成并包含租户域；用户不得提交 key。"""
        aid = str(artifact_id)
        return f"tenants/{tenant_id}/artifacts/{kind}/{aid[:2]}/{aid}"

    async def store_object(
        self,
        tenant_id: uuid.UUID,
        *,
        kind: str,
        mime: str | None,
        content_hash: str,
        size_bytes: int,
        profile: ProfileBinding,
        chunks_factory: Callable[[], Iterable[bytes]],
        content_type: str | None = None,
    ) -> ArtifactInfo:
        """登记 pending location -> 写入对象 -> CAS 标记 active。"""
        if profile.write_disabled:
            raise StorageUnavailableError("该存储 profile 已禁用写入")
        artifact_id = uuid.uuid4()
        object_key = self.build_object_key(tenant_id, kind, artifact_id)
        target = ObjectTarget(
            profile_revision_id=profile.profile_id or uuid.uuid5(uuid.NAMESPACE_URL, profile.name),
            bucket=profile.bucket,
            root_ref=profile.root_ref,
            key=object_key,
        )

        async with self._uow.begin() as session:
            artifact = ArtifactRow(
                id=artifact_id,
                tenant_id=tenant_id,
                content_hash=content_hash,
                size_bytes=size_bytes,
                mime=mime,
                kind=kind,
            )
            session.add(artifact)
            location = ArtifactLocationRow(
                tenant_id=tenant_id,
                artifact_id=artifact_id,
                profile_revision_id=profile.profile_id,
                provider=profile.provider,
                bucket=profile.bucket,
                root_ref=profile.root_ref,
                object_key=object_key,
                size_bytes=size_bytes,
                checksum_sha256=content_hash,
                state="pending",
            )
            session.add(location)
            await session.flush()
            location_id = location.id

        try:
            stored = profile.backend.put(
                target,
                chunks_factory(),
                PutOptions(
                    content_type=content_type or mime,
                    expected_sha256=content_hash,
                    max_bytes=None,
                ),
            )
        except StorageChecksumMismatch as exc:
            await self._mark_location_failed(location_id)
            raise ChecksumMismatchError("内容校验失败") from exc
        except (
            StorageQuotaExceeded,
            StorageNotFound,
            StorageUnavailable,
            UnsupportedCapability,
        ) as exc:
            await self._mark_location_failed(location_id)
            raise StorageUnavailableError("对象写入失败") from exc
        except StorageError as exc:  # pragma: no cover - 兜底映射
            await self._mark_location_failed(location_id)
            raise StorageUnavailableError("对象写入失败") from exc

        async with self._uow.begin() as session:
            location = (
                await session.execute(
                    select(ArtifactLocationRow).where(ArtifactLocationRow.id == location_id)
                )
            ).scalar_one()
            if stored.sha256 and stored.sha256 != content_hash:
                raise ChecksumMismatchError("写入校验失败")
            location.state = "active"
            location.object_version = stored.locator.object_version
            artifact = (
                await session.execute(select(ArtifactRow).where(ArtifactRow.id == artifact_id))
            ).scalar_one()
            artifact.preferred_location_id = location_id
            return self._artifact_info(artifact)

    async def _mark_location_failed(self, location_id: uuid.UUID) -> None:
        async with self._uow.begin() as session:
            location = (
                await session.execute(
                    select(ArtifactLocationRow).where(ArtifactLocationRow.id == location_id)
                )
            ).scalar_one_or_none()
            if location is not None:
                location.state = "failed"

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    async def resolve_reader(
        self,
        tenant_id: uuid.UUID,
        artifact_id: uuid.UUID,
        byte_range: ByteRange | None = None,
    ) -> tuple[LocationInfo, ProfileBinding]:
        location, profile = await self.resolve_location(tenant_id, artifact_id)
        return location, profile

    async def resolve_location(
        self, tenant_id: uuid.UUID, artifact_id: uuid.UUID
    ) -> tuple[LocationInfo, ProfileBinding]:
        """已存在对象按 location 的原 profile revision 读取。"""
        async with self._uow.read_only() as session:
            artifact = (
                await session.execute(
                    select(ArtifactRow).where(
                        ArtifactRow.id == artifact_id, ArtifactRow.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if artifact is None:
                raise NotFoundError("产物不存在")
            location_row: ArtifactLocationRow | None = None
            if artifact.preferred_location_id is not None:
                location_row = (
                    await session.execute(
                        select(ArtifactLocationRow).where(
                            ArtifactLocationRow.id == artifact.preferred_location_id,
                            ArtifactLocationRow.state == "active",
                        )
                    )
                ).scalar_one_or_none()
            if location_row is None:
                location_row = (
                    await session.execute(
                        select(ArtifactLocationRow)
                        .where(
                            ArtifactLocationRow.artifact_id == artifact_id,
                            ArtifactLocationRow.state == "active",
                        )
                        .order_by(ArtifactLocationRow.created_at)
                        .limit(1)
                    )
                ).scalar_one_or_none()
            if location_row is None:
                raise NotFoundError("产物没有可用位置")
            location = self._location_info(location_row)
            profile_id = location_row.profile_revision_id
        if profile_id is not None:
            profile = await self._router.resolve_by_id(profile_id)
        else:
            profile = self._router.resolve_boot_local()
        return location, profile

    def open_reader(
        self,
        location: LocationInfo,
        profile: ProfileBinding,
        byte_range: ByteRange | None = None,
    ) -> Any:
        locator = ObjectLocator(
            profile_revision_id=location.profile_revision_id or uuid.uuid5(uuid.NAMESPACE_URL, profile.name),
            bucket=location.bucket,
            root_ref=location.root_ref,
            key=location.object_key,
            object_version=location.object_version,
        )
        try:
            return profile.backend.open_reader(locator, byte_range)
        except StorageNotFound as exc:
            raise NotFoundError("对象不存在") from exc
        except StorageError as exc:
            raise StorageUnavailableError("存储暂不可用") from exc

    # ------------------------------------------------------------------
    # 查询与引用计数
    # ------------------------------------------------------------------

    async def get_artifact(self, tenant_id: uuid.UUID, artifact_id: uuid.UUID) -> ArtifactInfo:
        async with self._uow.read_only() as session:
            artifact = (
                await session.execute(
                    select(ArtifactRow).where(
                        ArtifactRow.id == artifact_id, ArtifactRow.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if artifact is None:
                raise NotFoundError("产物不存在")
            return self._artifact_info(artifact)

    async def increment_ref(self, session: Any, artifact_id: uuid.UUID) -> None:
        from sqlalchemy import update

        await session.execute(
            update(ArtifactRow)
            .where(ArtifactRow.id == artifact_id)
            .values(ref_count=ArtifactRow.ref_count + 1)
        )

    async def decrement_ref(self, session: Any, artifact_id: uuid.UUID) -> None:
        from sqlalchemy import update

        await session.execute(
            update(ArtifactRow)
            .where(ArtifactRow.id == artifact_id)
            .values(ref_count=ArtifactRow.ref_count - 1)
        )

    async def purge_artifact(self, tenant_id: uuid.UUID, artifact_id: uuid.UUID) -> bool:
        """删除无引用 artifact：先删对象再删登记；对象删除失败保留登记并返回 False。

        调用方必须先确认没有 doc_version 引用（ref_count 可能滞后，不能单独作为依据）。
        """
        async with self._uow.read_only() as session:
            artifact = (
                await session.execute(
                    select(ArtifactRow).where(
                        ArtifactRow.id == artifact_id, ArtifactRow.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if artifact is None:
                return True
            rows = (
                (
                    await session.execute(
                        select(ArtifactLocationRow).where(ArtifactLocationRow.artifact_id == artifact_id)
                    )
                )
                .scalars()
                .all()
            )
            locations = [self._location_info(row) for row in rows]
        for location in locations:
            if location.profile_revision_id is not None:
                profile = await self._router.resolve_by_id(location.profile_revision_id)
            else:
                profile = self._router.resolve_boot_local()
            locator = ObjectLocator(
                profile_revision_id=location.profile_revision_id
                or uuid.uuid5(uuid.NAMESPACE_URL, profile.name),
                bucket=location.bucket,
                root_ref=location.root_ref,
                key=location.object_key,
                object_version=location.object_version,
            )
            try:
                profile.backend.delete(locator)
            except StorageNotFound:
                continue
            except StorageError:
                return False
        async with self._uow.begin() as session:
            await session.execute(
                delete(ArtifactLocationRow).where(ArtifactLocationRow.artifact_id == artifact_id)
            )
            await session.execute(
                delete(ArtifactRow).where(ArtifactRow.id == artifact_id, ArtifactRow.tenant_id == tenant_id)
            )
        return True

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _artifact_info(row: ArtifactRow) -> ArtifactInfo:
        return ArtifactInfo(
            id=row.id,
            tenant_id=row.tenant_id,
            content_hash=row.content_hash,
            size_bytes=row.size_bytes,
            mime=row.mime,
            kind=row.kind,
            ref_count=row.ref_count,
        )

    @staticmethod
    def _location_info(row: ArtifactLocationRow) -> LocationInfo:
        return LocationInfo(
            id=row.id,
            artifact_id=row.artifact_id,
            provider=row.provider,
            bucket=row.bucket,
            root_ref=row.root_ref,
            object_key=row.object_key,
            object_version=row.object_version,
            size_bytes=row.size_bytes,
            checksum_sha256=row.checksum_sha256,
            state=row.state,
            profile_revision_id=row.profile_revision_id,
        )
