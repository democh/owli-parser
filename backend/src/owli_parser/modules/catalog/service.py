"""catalog 应用服务：数据集、文档、版本与发布指针。

- 本服务只接受已经解析出的访问范围（CatalogScope），不自行认证。
- 删除为 tombstone 语义：先撤销读取（acl_revision 递增 + deleted_at），
  再异步清理索引与对象。
- 发布指针（visible_version_id）只能通过 publish_version 的 CAS 更新。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from owli_parser.core.clock import Clock
from owli_parser.core.context import ActorKind
from owli_parser.core.errors import (
    AlreadyExistsError,
    ForbiddenError,
    InvalidInputError,
    NotFoundError,
    RevisionConflictError,
    ValidationFailedError,
)
from owli_parser.core.pagination import decode_cursor, encode_cursor
from owli_parser.infrastructure.uow import UnitOfWork
from owli_parser.modules.catalog import domain
from owli_parser.modules.catalog.domain import UserAccessContext
from owli_parser.modules.catalog.models import DatasetRow, DocumentRow, DocVersionRow


@dataclass(frozen=True, slots=True)
class CatalogScope:
    """调用者的数据访问范围（由 API 层基于认证结果构造）。

    - actor_id：用户 ID 或应用 ID；用于"同主体"校验（如上传/导入会话归属）。
    """

    tenant_id: uuid.UUID
    kind: ActorKind
    actor_id: uuid.UUID | None = None
    user_ctx: UserAccessContext | None = None
    application_dataset_actions: dict[uuid.UUID, set[str]] | None = None

    def app_actions(self, dataset_id: uuid.UUID) -> set[str]:
        if self.application_dataset_actions is None:
            return set()
        return self.application_dataset_actions.get(dataset_id, set())


@dataclass(frozen=True, slots=True)
class DatasetInfo:
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    description: str | None
    owner_user_id: uuid.UUID
    visibility: str
    auth_revision: int
    revision: int
    chunk_policy: dict[str, Any]
    storage_profile_id: uuid.UUID | None
    allow_partial: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DocumentInfo:
    id: uuid.UUID
    tenant_id: uuid.UUID
    dataset_id: uuid.UUID
    filename: str
    mime: str | None
    size_bytes: int | None
    content_hash: str | None
    visible_version_id: uuid.UUID | None
    desired_version_id: uuid.UUID | None
    acl_revision: int
    revision: int
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class VersionInfo:
    id: uuid.UUID
    tenant_id: uuid.UUID
    document_id: uuid.UUID
    version_no: int
    source_artifact_id: uuid.UUID
    ir_artifact_id: uuid.UUID | None
    lifecycle: str
    effective_config_digest: str
    coverage: dict[str, Any] | None
    quality_report: dict[str, Any] | None
    created_at: datetime


class CatalogService:
    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    # ------------------------------------------------------------------
    # 数据集
    # ------------------------------------------------------------------

    async def create_dataset(
        self,
        scope: CatalogScope,
        *,
        name: str,
        description: str | None,
        visibility: str,
        storage_profile_id: uuid.UUID | None = None,
        chunk_policy: dict[str, Any] | None = None,
    ) -> DatasetInfo:
        ctx = self._require_user(scope)
        if not domain.can_create_dataset(ctx):
            raise ForbiddenError("当前角色不允许创建数据集")
        if visibility not in ("tenant", "private"):
            raise InvalidInputError("visibility 只允许 tenant/private")
        clean = name.strip()
        if not 1 <= len(clean) <= 128:
            raise ValidationFailedError("数据集名称长度需在 1-128 之间")
        async with self._uow.begin() as session:
            row = DatasetRow(
                tenant_id=scope.tenant_id,
                name=clean,
                description=description,
                owner_user_id=ctx.user_id,
                visibility=visibility,
                chunk_policy=chunk_policy or {},
                storage_profile_id=storage_profile_id,
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise AlreadyExistsError("数据集名称已存在") from exc
            return self._dataset_info(row)

    async def list_datasets(
        self, scope: CatalogScope, *, cursor: str | None, limit: int
    ) -> tuple[list[DatasetInfo], str | None, bool]:
        conditions = [DatasetRow.tenant_id == scope.tenant_id, DatasetRow.deleted_at.is_(None)]
        if scope.kind is ActorKind.SYSTEM:
            pass  # 系统主体：返回本租户全部数据集
        elif scope.kind is ActorKind.APPLICATION:
            allowed = list((scope.application_dataset_actions or {}).keys())
            if not allowed:
                return [], None, False
            conditions.append(DatasetRow.id.in_(allowed))
        else:
            ctx = self._require_user(scope)
            if not ctx.is_admin:
                conditions.append(
                    or_(
                        DatasetRow.visibility == "tenant",
                        DatasetRow.owner_user_id == ctx.user_id,
                    )
                )
        if cursor:
            data = decode_cursor(cursor)
            created_at = data.get("ts")
            last_id = data.get("id")
            if created_at and last_id:
                conditions.append(
                    or_(
                        DatasetRow.created_at > datetime.fromisoformat(str(created_at)),
                        (DatasetRow.created_at == datetime.fromisoformat(str(created_at)))
                        & (DatasetRow.id > uuid.UUID(str(last_id))),
                    )
                )
        async with self._uow.begin() as session:
            rows = (
                (
                    await session.execute(
                        select(DatasetRow)
                        .where(*conditions)
                        .order_by(DatasetRow.created_at, DatasetRow.id)
                        .limit(limit + 1)
                    )
                )
                .scalars()
                .all()
            )
        return self._page(rows, limit, lambda r: {"ts": r.created_at.isoformat(), "id": str(r.id)})

    async def get_dataset(self, scope: CatalogScope, dataset_id: uuid.UUID) -> DatasetInfo:
        async with self._uow.begin() as session:
            row = await self._get_dataset_row(session, scope.tenant_id, dataset_id)
            self._ensure_read(scope, row)
            return self._dataset_info(row)

    async def update_dataset(
        self,
        scope: CatalogScope,
        dataset_id: uuid.UUID,
        *,
        expected_revision: int,
        name: str | None = None,
        description: str | None = None,
        visibility: str | None = None,
        allow_partial: bool | None = None,
        chunk_policy: dict[str, Any] | None = None,
        storage_profile_id: uuid.UUID | None = None,
    ) -> DatasetInfo:
        async with self._uow.begin() as session:
            row = await self._get_dataset_row(session, scope.tenant_id, dataset_id)
            self._ensure_manage(scope, row)
            if row.revision != expected_revision:
                raise RevisionConflictError("数据集已被其他操作修改")
            if name is not None:
                clean = name.strip()
                if not 1 <= len(clean) <= 128:
                    raise ValidationFailedError("数据集名称长度需在 1-128 之间")
                row.name = clean
            if description is not None:
                row.description = description
            if visibility is not None:
                if visibility not in ("tenant", "private"):
                    raise InvalidInputError("visibility 只允许 tenant/private")
                row.visibility = visibility
                row.auth_revision += 1  # 可见性变化立即影响读取授权
            if allow_partial is not None:
                row.allow_partial = allow_partial
            if chunk_policy is not None:
                row.chunk_policy = chunk_policy
            if storage_profile_id is not None:
                row.storage_profile_id = storage_profile_id
            row.revision += 1
            return self._dataset_info(row)

    async def delete_dataset(self, scope: CatalogScope, dataset_id: uuid.UUID) -> None:
        """软删除：立即撤销读取；对象与索引由后续清理任务回收。"""
        now = self._clock.now()
        async with self._uow.begin() as session:
            row = await self._get_dataset_row(session, scope.tenant_id, dataset_id)
            self._ensure_manage(scope, row)
            row.deleted_at = now
            row.auth_revision += 1
            row.revision += 1
            # 数据集内文档同步 tombstone（可见读取立即失效）
            await session.execute(
                update(DocumentRow)
                .where(DocumentRow.dataset_id == dataset_id, DocumentRow.deleted_at.is_(None))
                .values(deleted_at=now, acl_revision=DocumentRow.acl_revision + 1)
            )

    async def require_dataset_read(self, scope: CatalogScope, dataset_id: uuid.UUID) -> None:
        async with self._uow.read_only() as session:
            row = await self._get_dataset_row(session, scope.tenant_id, dataset_id)
            self._ensure_read(scope, row)

    async def require_dataset_write(self, scope: CatalogScope, dataset_id: uuid.UUID) -> None:
        async with self._uow.read_only() as session:
            row = await self._get_dataset_row(session, scope.tenant_id, dataset_id)
            self._ensure_write(scope, row)

    # ------------------------------------------------------------------
    # 文档与版本
    # ------------------------------------------------------------------

    async def create_document_with_version(
        self,
        scope: CatalogScope,
        *,
        dataset_id: uuid.UUID,
        filename: str,
        mime: str | None,
        size_bytes: int | None,
        content_hash: str,
        source_artifact_id: uuid.UUID,
        effective_config_digest: str,
        chunk_policy: dict[str, Any],
        tokenizer_revision: str | None,
        actor_id: uuid.UUID,
        document_id: uuid.UUID | None = None,
        expected_revision: int | None = None,
        parse_only: bool = False,
    ) -> tuple[DocumentInfo, VersionInfo]:
        """登记候选文档版本（不发布）+ 更新 desired_version_id。

        更新已有文档必须校验 expected_revision，防止并发覆盖。
        """
        async with self._uow.begin() as session:
            dataset = await self._get_dataset_row(session, scope.tenant_id, dataset_id)
            self._ensure_write(scope, dataset)
            if document_id is None:
                doc = DocumentRow(
                    tenant_id=scope.tenant_id,
                    dataset_id=dataset_id,
                    filename=filename[:512],
                    mime=mime,
                    size_bytes=size_bytes,
                    content_hash=content_hash,
                    created_by_kind=str(scope.kind),
                    created_by_id=actor_id,
                )
                session.add(doc)
                await session.flush()
                await session.refresh(doc)
                version_no = 1
            else:
                existing_doc = (
                    await session.execute(
                        select(DocumentRow).where(
                            DocumentRow.id == document_id,
                            DocumentRow.tenant_id == scope.tenant_id,
                            DocumentRow.dataset_id == dataset_id,
                        )
                    )
                ).scalar_one_or_none()
                if existing_doc is None or existing_doc.deleted_at is not None:
                    raise NotFoundError("文档不存在")
                doc = existing_doc
                if expected_revision is not None and doc.revision != expected_revision:
                    raise RevisionConflictError("文档已被其他操作修改")
                max_no = (
                    await session.execute(
                        select(func.max(DocVersionRow.version_no)).where(DocVersionRow.document_id == doc.id)
                    )
                ).scalar_one()
                version_no = (max_no or 0) + 1
                doc.filename = filename[:512]
                doc.mime = mime
                doc.size_bytes = size_bytes
                doc.content_hash = content_hash
                doc.revision += 1

            version = DocVersionRow(
                tenant_id=scope.tenant_id,
                document_id=doc.id,
                version_no=version_no,
                source_artifact_id=source_artifact_id,
                lifecycle="candidate",
                effective_config_digest=effective_config_digest,
                chunk_policy=chunk_policy,
                tokenizer_revision=tokenizer_revision,
                created_by_kind=str(scope.kind),
                created_by_id=actor_id,
            )
            session.add(version)
            await session.flush()
            # 新候选版本不得立即覆盖可见版本
            doc.desired_version_id = version.id
            await session.flush()
            return self._doc_info(doc), self._version_info(version)

    async def list_documents(
        self,
        scope: CatalogScope,
        *,
        dataset_id: uuid.UUID | None,
        cursor: str | None,
        limit: int,
        include_deleted: bool = False,
    ) -> tuple[list[DocumentInfo], str | None, bool]:
        dataset_ids = await self._readable_dataset_ids(scope, dataset_id)
        if not dataset_ids:
            return [], None, False
        conditions = [
            DocumentRow.tenant_id == scope.tenant_id,
            DocumentRow.dataset_id.in_(dataset_ids),
        ]
        if not include_deleted:
            conditions.append(DocumentRow.deleted_at.is_(None))
        if cursor:
            data = decode_cursor(cursor)
            created_at = data.get("ts")
            last_id = data.get("id")
            if created_at and last_id:
                conditions.append(
                    or_(
                        DocumentRow.created_at < datetime.fromisoformat(str(created_at)),
                        (DocumentRow.created_at == datetime.fromisoformat(str(created_at)))
                        & (DocumentRow.id > uuid.UUID(str(last_id))),
                    )
                )
        async with self._uow.begin() as session:
            rows = (
                (
                    await session.execute(
                        select(DocumentRow)
                        .where(*conditions)
                        .order_by(DocumentRow.created_at.desc(), DocumentRow.id)
                        .limit(limit + 1)
                    )
                )
                .scalars()
                .all()
            )
        return self._page(rows, limit, lambda r: {"ts": r.created_at.isoformat(), "id": str(r.id)})

    async def get_document(self, scope: CatalogScope, document_id: uuid.UUID) -> DocumentInfo:
        async with self._uow.begin() as session:
            row = await self._get_document_row(session, scope.tenant_id, document_id)
            await self._ensure_document_read(session, scope, row)
            return self._doc_info(row)

    async def list_versions(self, scope: CatalogScope, document_id: uuid.UUID) -> list[VersionInfo]:
        async with self._uow.begin() as session:
            doc = await self._get_document_row(session, scope.tenant_id, document_id)
            await self._ensure_document_read(session, scope, doc)
            rows = (
                (
                    await session.execute(
                        select(DocVersionRow)
                        .where(DocVersionRow.document_id == document_id)
                        .order_by(DocVersionRow.version_no.desc())
                    )
                )
                .scalars()
                .all()
            )
            return [self._version_info(r) for r in rows]

    async def get_version(
        self,
        scope: CatalogScope,
        document_id: uuid.UUID,
        version_id: uuid.UUID | None,
        *,
        fallback_latest: bool = False,
    ) -> VersionInfo:
        """version_id 为空时取当前 visible_version。

        fallback_latest=True 时（预览原件等场景），无已发布版本则依次回退
        desired_version 与最新候选版本——“可以预览”不代表“已解析/已发布”。
        """
        async with self._uow.begin() as session:
            doc = await self._get_document_row(session, scope.tenant_id, document_id)
            await self._ensure_document_read(session, scope, doc)
            target = version_id or doc.visible_version_id
            if target is None and fallback_latest:
                target = doc.desired_version_id
            if target is None and fallback_latest:
                target = (
                    await session.execute(
                        select(DocVersionRow.id)
                        .where(DocVersionRow.document_id == document_id)
                        .order_by(DocVersionRow.version_no.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
            if target is None:
                raise NotFoundError("文档尚未发布任何可用版本")
            row = (
                await session.execute(
                    select(DocVersionRow).where(
                        DocVersionRow.id == target, DocVersionRow.document_id == document_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("版本不存在")
            return self._version_info(row)

    async def rename_document(
        self, scope: CatalogScope, document_id: uuid.UUID, *, filename: str, expected_revision: int
    ) -> DocumentInfo:
        clean = filename.strip()
        if not 1 <= len(clean) <= 512:
            raise ValidationFailedError("文件名长度需在 1-512 之间")
        async with self._uow.begin() as session:
            doc = await self._get_document_row(session, scope.tenant_id, document_id)
            await self._ensure_document_read(session, scope, doc)
            await self._ensure_document_write(session, scope, doc)
            if doc.revision != expected_revision:
                raise RevisionConflictError("文档已被其他操作修改")
            doc.filename = clean
            doc.revision += 1
            return self._doc_info(doc)

    async def delete_document(self, scope: CatalogScope, document_id: uuid.UUID) -> None:
        now = self._clock.now()
        async with self._uow.begin() as session:
            doc = await self._get_document_row(session, scope.tenant_id, document_id)
            await self._ensure_document_delete(session, scope, doc)
            doc.deleted_at = now
            doc.acl_revision += 1
            doc.revision += 1

    async def publish_version(
        self,
        tenant_id: uuid.UUID,
        *,
        document_id: uuid.UUID,
        version_id: uuid.UUID,
        expected_desired_version: uuid.UUID | None,
    ) -> bool:
        """发布协议第 5 步：CAS 更新 visible_version。

        返回 False 表示 desired_version 已变化或文档已删除（放弃发布）。
        """
        async with self._uow.begin() as session:
            doc = (
                await session.execute(
                    select(DocumentRow).where(
                        DocumentRow.id == document_id, DocumentRow.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if doc is None or doc.deleted_at is not None:
                return False
            if expected_desired_version is not None and doc.desired_version_id != expected_desired_version:
                return False
            doc.visible_version_id = version_id
            doc.revision += 1
            version = (
                await session.execute(select(DocVersionRow).where(DocVersionRow.id == version_id))
            ).scalar_one_or_none()
            if version is not None:
                version.lifecycle = "published"
                # 旧版本标记 superseded（保留其索引与引用，便于回滚）
                await session.execute(
                    update(DocVersionRow)
                    .where(
                        DocVersionRow.document_id == document_id,
                        DocVersionRow.id != version_id,
                        DocVersionRow.lifecycle == "published",
                    )
                    .values(lifecycle="superseded")
                )
            return True

    async def set_version_ir(
        self,
        version_id: uuid.UUID,
        *,
        ir_artifact_id: uuid.UUID,
        coverage: dict[str, Any],
        quality_report: dict[str, Any],
        lifecycle: str = "parsed",
    ) -> None:
        async with self._uow.begin() as session:
            version = (
                await session.execute(select(DocVersionRow).where(DocVersionRow.id == version_id))
            ).scalar_one_or_none()
            if version is None:
                raise NotFoundError("版本不存在")
            version.ir_artifact_id = ir_artifact_id
            version.coverage = coverage
            version.quality_report = quality_report
            version.lifecycle = lifecycle

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _require_user(self, scope: CatalogScope) -> UserAccessContext:
        if scope.user_ctx is None:
            raise ForbiddenError("该操作需要用户身份")
        return scope.user_ctx

    def _ensure_read(self, scope: CatalogScope, dataset: DatasetRow) -> None:
        if scope.kind is ActorKind.SYSTEM:
            return  # 系统主体：租户归属已由查询条件强制；仅用于服务端编排
        if scope.kind is ActorKind.APPLICATION:
            if "read" not in scope.app_actions(dataset.id):
                raise NotFoundError("数据集不存在或不可用")
            return
        ctx = self._require_user(scope)
        if not domain.can_read_dataset(
            ctx, visibility=dataset.visibility, owner_user_id=dataset.owner_user_id
        ):
            raise NotFoundError("数据集不存在或不可用")

    def _ensure_write(self, scope: CatalogScope, dataset: DatasetRow) -> None:
        if scope.kind is ActorKind.SYSTEM:
            return
        if scope.kind is ActorKind.APPLICATION:
            if "write" not in scope.app_actions(dataset.id):
                raise ForbiddenError("应用未被授予该数据集写权限")
            return
        ctx = self._require_user(scope)
        if not domain.can_write_dataset(
            ctx, visibility=dataset.visibility, owner_user_id=dataset.owner_user_id
        ):
            raise ForbiddenError("当前角色不允许修改该数据集内容")

    def _ensure_manage(self, scope: CatalogScope, dataset: DatasetRow) -> None:
        if scope.kind is ActorKind.APPLICATION:
            raise ForbiddenError("外部应用不能管理数据集设置")
        ctx = self._require_user(scope)
        if not domain.can_manage_dataset(ctx, owner_user_id=dataset.owner_user_id):
            raise ForbiddenError("只有数据集创建者或管理员可以执行该操作")

    async def _ensure_document_read(self, session: object, scope: CatalogScope, doc: DocumentRow) -> None:
        """文档读取权限继承自数据集读取权限。"""
        if doc.deleted_at is not None:
            raise NotFoundError("文档已被删除")
        dataset = await self._get_dataset_row(session, scope.tenant_id, doc.dataset_id)
        self._ensure_read(scope, dataset)

    async def _ensure_document_write(self, session: object, scope: CatalogScope, doc: DocumentRow) -> None:
        dataset = await self._get_dataset_row(session, scope.tenant_id, doc.dataset_id)
        self._ensure_write(scope, dataset)

    async def _ensure_document_delete(self, session: object, scope: CatalogScope, doc: DocumentRow) -> None:
        if scope.kind is ActorKind.APPLICATION:
            if "delete" not in scope.app_actions(doc.dataset_id):
                raise ForbiddenError("应用未被授予该数据集 delete 权限")
            return
        await self._ensure_document_write(session, scope, doc)

    async def _readable_dataset_ids(
        self, scope: CatalogScope, dataset_id: uuid.UUID | None
    ) -> list[uuid.UUID]:
        async with self._uow.read_only() as session:
            conditions = [
                DatasetRow.tenant_id == scope.tenant_id,
                DatasetRow.deleted_at.is_(None),
            ]
            if dataset_id is not None:
                conditions.append(DatasetRow.id == dataset_id)
            if scope.kind is ActorKind.SYSTEM:
                pass  # 系统主体：租户条件已限定范围；仅用于目录枚举
            elif scope.kind is ActorKind.APPLICATION:
                allowed = list((scope.application_dataset_actions or {}).keys())
                if not allowed:
                    return []
                conditions.append(DatasetRow.id.in_(allowed))
            else:
                ctx = self._require_user(scope)
                if not ctx.is_admin:
                    conditions.append(
                        or_(
                            DatasetRow.visibility == "tenant",
                            DatasetRow.owner_user_id == ctx.user_id,
                        )
                    )
            rows = (await session.execute(select(DatasetRow.id).where(*conditions))).scalars().all()
            return list(rows)

    async def _get_dataset_row(
        self, session: object, tenant_id: uuid.UUID, dataset_id: uuid.UUID
    ) -> DatasetRow:
        row = (
            await session.execute(  # type: ignore[attr-defined]
                select(DatasetRow).where(
                    DatasetRow.id == dataset_id,
                    DatasetRow.tenant_id == tenant_id,
                    DatasetRow.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("数据集不存在或不可用")
        return cast(DatasetRow, row)

    async def _get_document_row(
        self, session: object, tenant_id: uuid.UUID, document_id: uuid.UUID
    ) -> DocumentRow:
        row = (
            await session.execute(  # type: ignore[attr-defined]
                select(DocumentRow).where(DocumentRow.id == document_id, DocumentRow.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("文档不存在或不可用")
        return cast(DocumentRow, row)

    @staticmethod
    def _page(rows: Sequence[Any], limit: int, cursor_of: Any) -> tuple[list[Any], str | None, bool]:
        has_more = len(rows) > limit
        items = list(rows[:limit])
        next_cursor = encode_cursor(cursor_of(items[-1])) if has_more and items else None
        return items, next_cursor, has_more

    @staticmethod
    def _dataset_info(row: DatasetRow) -> DatasetInfo:
        return DatasetInfo(
            id=row.id,
            tenant_id=row.tenant_id,
            name=row.name,
            description=row.description,
            owner_user_id=row.owner_user_id,
            visibility=row.visibility,
            auth_revision=row.auth_revision,
            revision=row.revision,
            chunk_policy=row.chunk_policy or {},
            storage_profile_id=row.storage_profile_id,
            allow_partial=row.allow_partial,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _doc_info(row: DocumentRow) -> DocumentInfo:
        return DocumentInfo(
            id=row.id,
            tenant_id=row.tenant_id,
            dataset_id=row.dataset_id,
            filename=row.filename,
            mime=row.mime,
            size_bytes=row.size_bytes,
            content_hash=row.content_hash,
            visible_version_id=row.visible_version_id,
            desired_version_id=row.desired_version_id,
            acl_revision=row.acl_revision,
            revision=row.revision,
            deleted_at=row.deleted_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _version_info(row: DocVersionRow) -> VersionInfo:
        return VersionInfo(
            id=row.id,
            tenant_id=row.tenant_id,
            document_id=row.document_id,
            version_no=row.version_no,
            source_artifact_id=row.source_artifact_id,
            ir_artifact_id=row.ir_artifact_id,
            lifecycle=row.lifecycle,
            effective_config_digest=row.effective_config_digest,
            coverage=row.coverage,
            quality_report=row.quality_report,
            created_at=row.created_at,
        )
