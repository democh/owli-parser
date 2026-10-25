"""knowledge 应用服务：chunk 持久化、人工修订、索引清单。

- 修改创建 chunk revision（If-Match 控制并发）；受影响向量与词法索引都需重建。
- 禁用立即在权威层生效；重索引完成前不得用旧向量搭配新正文。
- index_manifest：确认必需索引完整后才允许发布；M1/M2 parse-only 的必需
  索引集合为空并明确 retrieval_ready=false。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from owli_parser.bootstrap.settings import ChunkSection
from owli_parser.contracts.ir import DocumentIR
from owli_parser.core.clock import Clock
from owli_parser.core.errors import (
    ConflictError,
    NotFoundError,
    RevisionConflictError,
    ValidationFailedError,
)
from owli_parser.core.pagination import encode_cursor
from owli_parser.infrastructure.uow import UnitOfWork
from owli_parser.modules.catalog.models import DocVersionRow
from owli_parser.modules.knowledge import chunking
from owli_parser.modules.knowledge.chunking import NewChunk
from owli_parser.modules.knowledge.models import (
    ChunkRevisionRow,
    ChunkRow,
    GoldenRunRow,
    GoldenSetRow,
    IndexActionRow,
    IndexManifestRow,
    ReviewItemRow,
)
from owli_parser.ports.outbox import OutboxRecorder


@dataclass(frozen=True, slots=True)
class ChunkInfo:
    id: uuid.UUID
    version_id: uuid.UUID
    doc_id: uuid.UUID
    dataset_id: uuid.UUID
    ordinal: int
    kind: str
    parent_id: uuid.UUID | None
    current_revision: int
    text: str
    token_count: int
    source_refs: list[dict[str, Any]]
    explanation: dict[str, Any] | None
    structure: dict[str, Any]
    enabled: bool
    embedding_state: str
    acl_ref: str


@dataclass(frozen=True, slots=True)
class ManifestInfo:
    id: uuid.UUID
    version_id: uuid.UUID
    index_generation: int
    required_channels: list[str]
    expected_count: int
    state: str
    acknowledgements: dict[str, Any]


@dataclass(frozen=True, slots=True)
class IndexActionInfo:
    id: uuid.UUID
    tenant_id: uuid.UUID
    chunk_id: uuid.UUID
    version_id: uuid.UUID
    desired_revision: int
    action: str
    state: str
    attempts: int
    last_error: str | None


@dataclass(frozen=True, slots=True)
class GoldenSetInfo:
    id: uuid.UUID
    tenant_id: uuid.UUID | None
    name: str
    cases: list[dict[str, Any]]
    expectations: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GoldenRunInfo:
    id: uuid.UUID
    golden_set_id: uuid.UUID
    golden_set_name: str | None
    run_id: str
    adapter_ref: str
    config_digest: str | None
    metrics: dict[str, Any]
    state: str
    created_at: Any
    # 黄金集租户（None = 全局 CLI 集）；详情端点据此做租户隔离
    tenant_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ReviewItemInfo:
    id: uuid.UUID
    version_id: uuid.UUID
    document_id: uuid.UUID | None  # 供前端直接跳转工作台
    job_id: uuid.UUID | None
    block_id: str
    reason: str
    status: str
    reviewer_id: uuid.UUID | None
    resolution: dict[str, Any] | None
    created_at: Any


class KnowledgeService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        chunk_settings: ChunkSection,
        outbox: OutboxRecorder | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._chunk_settings = chunk_settings
        self._outbox = outbox

    # ------------------------------------------------------------------
    # 分块持久化
    # ------------------------------------------------------------------

    async def create_chunks_for_version(
        self,
        *,
        tenant_id: uuid.UUID,
        dataset_id: uuid.UUID,
        doc_id: uuid.UUID,
        version_id: uuid.UUID,
        ir: DocumentIR,
        created_by_kind: str,
        created_by_id: uuid.UUID,
    ) -> list[uuid.UUID]:
        """在调用方事务外执行：切分 + 持久化（新版本首建，重复调用报冲突）。"""
        built = chunking.build_chunks(ir, self._chunk_settings)
        async with self._uow.begin() as session:
            existing = (
                await session.execute(
                    select(func.count()).select_from(ChunkRow).where(ChunkRow.version_id == version_id)
                )
            ).scalar_one()
            if existing:
                raise ConflictError("该版本已存在分块，禁止重复构建")
            return await self._persist(
                session, tenant_id, dataset_id, doc_id, version_id, built, created_by_kind, created_by_id
            )

    async def rebuild_chunks_for_version(
        self,
        *,
        tenant_id: uuid.UUID,
        dataset_id: uuid.UUID,
        doc_id: uuid.UUID,
        version_id: uuid.UUID,
        ir: DocumentIR,
        created_by_kind: str,
        created_by_id: uuid.UUID,
    ) -> list[uuid.UUID]:
        """重建：删除旧块（含历史 revision）后重新构建。"""
        from sqlalchemy import delete

        built = chunking.build_chunks(ir, self._chunk_settings)
        async with self._uow.begin() as session:
            old_ids = (
                (await session.execute(select(ChunkRow.id).where(ChunkRow.version_id == version_id)))
                .scalars()
                .all()
            )
            if old_ids:
                await session.execute(delete(ChunkRevisionRow).where(ChunkRevisionRow.chunk_id.in_(old_ids)))
                await session.execute(delete(ChunkRow).where(ChunkRow.id.in_(old_ids)))
            return await self._persist(
                session, tenant_id, dataset_id, doc_id, version_id, built, created_by_kind, created_by_id
            )

    async def _persist(
        self,
        session: Any,
        tenant_id: uuid.UUID,
        dataset_id: uuid.UUID,
        doc_id: uuid.UUID,
        version_id: uuid.UUID,
        built: list[NewChunk],
        created_by_kind: str,
        created_by_id: uuid.UUID,
    ) -> list[uuid.UUID]:
        acl_ref = f"dataset:{dataset_id}"
        id_by_ordinal: dict[int, uuid.UUID] = {}
        rows: list[ChunkRow] = []
        for chunk in built:
            row = ChunkRow(
                tenant_id=tenant_id,
                dataset_id=dataset_id,
                version_id=version_id,
                doc_id=doc_id,
                current_revision=1,
                parent_id=None,
                ordinal=chunk.ordinal,
                kind=chunk.kind,
                acl_ref=acl_ref,
                enabled=chunk.enabled,
                embedding_state="pending",
                structure={"heading_path": chunk.explanation.get("heading_path", [])}
                if chunk.explanation
                else {},
            )
            session.add(row)
            rows.append(row)
            id_by_ordinal[chunk.ordinal] = row.id
        await session.flush()
        for chunk, row in zip(built, rows, strict=True):
            if chunk.parent_ordinal is not None:
                row.parent_id = id_by_ordinal.get(chunk.parent_ordinal)
            session.add(
                ChunkRevisionRow(
                    chunk_id=row.id,
                    revision=1,
                    text=chunk.text,
                    token_count=chunk.token_count,
                    source_refs=chunk.source_refs,
                    explanation=chunk.explanation or None,
                    created_by_kind=created_by_kind,
                    created_by_id=created_by_id,
                )
            )
        await session.flush()
        return [row.id for row in rows]

    # ------------------------------------------------------------------
    # 查询与编辑
    # ------------------------------------------------------------------

    async def list_chunks(
        self,
        tenant_id: uuid.UUID,
        *,
        document_id: uuid.UUID,
        version_id: uuid.UUID | None,
        cursor: str | None,
        limit: int,
        include_disabled: bool = False,
    ) -> tuple[list[ChunkInfo], str | None, bool]:
        async with self._uow.read_only() as session:
            resolved_version = version_id
            if resolved_version is None:
                from owli_parser.modules.catalog.models import DocumentRow

                doc = (
                    await session.execute(
                        select(DocumentRow).where(
                            DocumentRow.id == document_id, DocumentRow.tenant_id == tenant_id
                        )
                    )
                ).scalar_one_or_none()
                if doc is None or doc.visible_version_id is None:
                    raise NotFoundError("文档没有可见版本")
                resolved_version = doc.visible_version_id
            conditions = [
                ChunkRow.tenant_id == tenant_id,
                ChunkRow.doc_id == document_id,
                ChunkRow.version_id == resolved_version,
            ]
            if not include_disabled:
                conditions.append(ChunkRow.enabled.is_(True))
            offset = 0
            if cursor:
                from owli_parser.core.pagination import decode_cursor

                data = decode_cursor(cursor)
                offset = int(data.get("offset", 0))
            rows = (
                (
                    await session.execute(
                        select(ChunkRow)
                        .where(*conditions)
                        .order_by(ChunkRow.ordinal, ChunkRow.id)
                        .offset(offset)
                        .limit(limit + 1)
                    )
                )
                .scalars()
                .all()
            )
            has_more = len(rows) > limit
            items = rows[:limit]
            infos = [await self._to_info(session, row) for row in items]
            next_cursor = encode_cursor({"offset": offset + limit}) if has_more else None
            return infos, next_cursor, has_more

    async def get_chunk(self, tenant_id: uuid.UUID, chunk_id: uuid.UUID) -> ChunkInfo:
        async with self._uow.read_only() as session:
            row = (
                await session.execute(
                    select(ChunkRow).where(ChunkRow.id == chunk_id, ChunkRow.tenant_id == tenant_id)
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("分块不存在")
            return await self._to_info(session, row)

    async def edit_chunk(
        self,
        tenant_id: uuid.UUID,
        chunk_id: uuid.UUID,
        *,
        expected_revision: int,
        text: str | None,
        enabled: bool | None,
        created_by_kind: str,
        created_by_id: uuid.UUID,
    ) -> ChunkInfo:
        """编辑创建新 revision；禁用立即生效并标记待重建索引。"""
        if text is None and enabled is None:
            raise ValidationFailedError("没有需要修改的字段")
        if text is not None and not text.strip():
            raise ValidationFailedError("正文不能为空")
        async with self._uow.begin() as session:
            row = (
                await session.execute(
                    select(ChunkRow).where(ChunkRow.id == chunk_id, ChunkRow.tenant_id == tenant_id)
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("分块不存在")
            if row.current_revision != expected_revision:
                raise RevisionConflictError("分块已被其他操作修改")
            previous = (
                await session.execute(
                    select(ChunkRevisionRow).where(
                        ChunkRevisionRow.chunk_id == chunk_id,
                        ChunkRevisionRow.revision == row.current_revision,
                    )
                )
            ).scalar_one()
            new_text = text if text is not None else previous.text
            new_revision = row.current_revision + 1
            session.add(
                ChunkRevisionRow(
                    chunk_id=chunk_id,
                    revision=new_revision,
                    text=new_text,
                    token_count=chunking.count_tokens(new_text, self._chunk_settings.tokenizer_ref),
                    source_refs=list(previous.source_refs or []),
                    explanation={
                        "kind": "manual_edit",
                        "base_revision": previous.revision,
                        "edited": text is not None,
                    },
                    created_by_kind=created_by_kind,
                    created_by_id=created_by_id,
                )
            )
            row.current_revision = new_revision
            if enabled is not None:
                row.enabled = enabled
            # 正文变化或禁用：旧向量不得搭配新正文，标记为待重建
            row.embedding_state = "pending"
            if text is not None:
                # 与编辑同事务登记重索引行动（不依赖前端再调用 reembed）；
                # 消费前旧索引条目由检索侧权威校验剔除，不会召回旧内容。
                action = IndexActionRow(
                    tenant_id=tenant_id,
                    chunk_id=chunk_id,
                    version_id=row.version_id,
                    desired_revision=new_revision,
                    action="upsert",
                    state="pending",
                )
                session.add(action)
                await session.flush()
                if self._outbox is not None:
                    await self._outbox.record(
                        aggregate_type="index_action",
                        aggregate_id=str(action.id),
                        event_type="index.apply",
                        payload={"action_id": str(action.id)},
                        session=session,
                    )
            await session.flush()
            return await self._to_info(session, row)

    async def inactive_chunk_ids(self, version_id: uuid.UUID) -> list[str]:
        async with self._uow.read_only() as session:
            rows = (
                (
                    await session.execute(
                        select(ChunkRow.id).where(
                            ChunkRow.version_id == version_id, ChunkRow.enabled.is_(False)
                        )
                    )
                )
                .scalars()
                .all()
            )
            return [str(r) for r in rows]

    # ------------------------------------------------------------------
    # 索引清单
    # ------------------------------------------------------------------

    async def create_manifest(
        self,
        *,
        tenant_id: uuid.UUID,
        version_id: uuid.UUID,
        index_generation: int,
        required_channels: list[str],
    ) -> ManifestInfo:
        """冻结 chunk revision 集与 index_generation；parse-only 时 channels 为空。"""
        async with self._uow.begin() as session:
            rows = (
                await session.execute(
                    select(ChunkRow.id, ChunkRow.current_revision).where(
                        ChunkRow.version_id == version_id, ChunkRow.enabled.is_(True)
                    )
                )
            ).all()
            expected_ids = sorted(f"{row[0]}:{row[1]}" for row in rows)
            manifest = IndexManifestRow(
                tenant_id=tenant_id,
                version_id=version_id,
                index_generation=index_generation,
                required_channels=required_channels,
                expected_ids_digest=chunking.canonical_ids_digest(expected_ids),
                expected_ids=expected_ids,
                expected_count=len(expected_ids),
                acknowledgements={},
                model_revisions={},
                state="ready" if not required_channels else "building",
            )
            session.add(manifest)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise ConflictError("该版本与索引代已存在清单") from exc
            return self._manifest_info(manifest)

    async def acknowledge_channel(
        self,
        manifest_id: uuid.UUID,
        *,
        channel: str,
        acknowledged_count: int,
        ids_digest: str,
        model_revision: str | None = None,
    ) -> ManifestInfo:
        """记录通道确认；digest 必须与冻结的 expected 集合一致。"""
        async with self._uow.begin() as session:
            manifest = (
                await session.execute(select(IndexManifestRow).where(IndexManifestRow.id == manifest_id))
            ).scalar_one_or_none()
            if manifest is None:
                raise NotFoundError("索引清单不存在")
            if channel not in (manifest.required_channels or []):
                raise ValidationFailedError("该通道不在必需集合内")
            if ids_digest != manifest.expected_ids_digest:
                raise ConflictError("确认集合与冻结清单不一致")
            if acknowledged_count < manifest.expected_count:
                raise ConflictError("索引尚未完整，禁止确认")
            acks = dict(manifest.acknowledgements or {})
            acks[channel] = {
                "count": acknowledged_count,
                "digest": ids_digest,
                "model_revision": model_revision,
                "acknowledged_at": self._clock.now().isoformat(),
            }
            manifest.acknowledgements = acks
            revisions = dict(manifest.model_revisions or {})
            if model_revision:
                revisions[channel] = model_revision
            manifest.model_revisions = revisions
            manifest.state = (
                "ready" if all(ch in acks for ch in (manifest.required_channels or [])) else "building"
            )
            await session.flush()
            return self._manifest_info(manifest)

    async def latest_manifest(self, version_id: uuid.UUID) -> ManifestInfo | None:
        async with self._uow.read_only() as session:
            manifest = (
                await session.execute(
                    select(IndexManifestRow)
                    .where(IndexManifestRow.version_id == version_id)
                    .order_by(IndexManifestRow.index_generation.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return self._manifest_info(manifest) if manifest else None

    # ------------------------------------------------------------------
    # 重索引行动（编辑派生；由 worker 消费）
    # ------------------------------------------------------------------

    async def get_index_action(self, action_id: uuid.UUID) -> IndexActionInfo | None:
        async with self._uow.read_only() as session:
            row = (
                await session.execute(select(IndexActionRow).where(IndexActionRow.id == action_id))
            ).scalar_one_or_none()
            return self._action_info(row) if row else None

    async def complete_index_action(self, action_id: uuid.UUID) -> None:
        async with self._uow.begin() as session:
            row = (
                await session.execute(select(IndexActionRow).where(IndexActionRow.id == action_id))
            ).scalar_one_or_none()
            if row is None:
                return
            row.state = "done"
            await session.flush()

    async def fail_index_action(
        self, action_id: uuid.UUID, *, error: str, max_attempts: int
    ) -> IndexActionInfo:
        """失败计数；未超限保持 pending（由 outbox 重派），超限置 failed。"""
        async with self._uow.begin() as session:
            row = (
                await session.execute(select(IndexActionRow).where(IndexActionRow.id == action_id))
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("索引行动不存在")
            row.attempts += 1
            row.last_error = error[:512]
            if row.attempts >= max_attempts:
                row.state = "failed"
            await session.flush()
            return self._action_info(row)

    async def resolve_index_actions_for_chunk(self, *, tenant_id: uuid.UUID, chunk_id: uuid.UUID) -> int:
        """重建完成后将同 chunk 的 pending 行动全部标记 done（自动与手动路径共用）。"""
        async with self._uow.begin() as session:
            rows = (
                (
                    await session.execute(
                        select(IndexActionRow).where(
                            IndexActionRow.tenant_id == tenant_id,
                            IndexActionRow.chunk_id == chunk_id,
                            IndexActionRow.state == "pending",
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.state = "done"
            await session.flush()
            return len(rows)

    # ------------------------------------------------------------------
    # 黄金集（检索回归；文档 §9）
    # ------------------------------------------------------------------

    async def upsert_golden_set(
        self,
        *,
        name: str,
        cases: list[dict[str, Any]],
        expectations: dict[str, Any] | None = None,
        tenant_id: uuid.UUID | None = None,
    ) -> GoldenSetInfo:
        """同名幂等更新（name 唯一）：cases/expectations 整体替换。"""
        async with self._uow.begin() as session:
            row = (
                await session.execute(select(GoldenSetRow).where(GoldenSetRow.name == name))
            ).scalar_one_or_none()
            if row is None:
                row = GoldenSetRow(
                    tenant_id=tenant_id, name=name, cases=cases, expectations=expectations or {}
                )
                session.add(row)
            else:
                row.cases = cases
                row.expectations = expectations or {}
                if tenant_id is not None:
                    row.tenant_id = tenant_id
            await session.flush()
            return self._golden_set_info(row)

    async def get_golden_set(self, name: str) -> GoldenSetInfo | None:
        async with self._uow.read_only() as session:
            row = (
                await session.execute(select(GoldenSetRow).where(GoldenSetRow.name == name))
            ).scalar_one_or_none()
            return self._golden_set_info(row) if row else None

    async def get_golden_set_by_id(self, golden_set_id: uuid.UUID) -> GoldenSetInfo | None:
        async with self._uow.read_only() as session:
            row = (
                await session.execute(select(GoldenSetRow).where(GoldenSetRow.id == golden_set_id))
            ).scalar_one_or_none()
            return self._golden_set_info(row) if row else None

    async def create_golden_run(
        self,
        *,
        golden_set_id: uuid.UUID,
        adapter_ref: str,
        config_digest: str | None,
        metrics: dict[str, Any],
        state: str,
    ) -> GoldenRunInfo:
        async with self._uow.begin() as session:
            row = GoldenRunRow(
                golden_set_id=golden_set_id,
                run_id=uuid.uuid4().hex,
                adapter_ref=adapter_ref,
                config_digest=config_digest,
                metrics=metrics,
                state=state,
            )
            session.add(row)
            await session.flush()
            golden = (
                await session.execute(
                    select(GoldenSetRow.name, GoldenSetRow.tenant_id).where(GoldenSetRow.id == golden_set_id)
                )
            ).first()
            return self._golden_run_info(
                row,
                golden_set_name=golden[0] if golden else None,
                tenant_id=golden[1] if golden else None,
            )

    async def get_golden_run(self, run_id: str) -> GoldenRunInfo | None:
        """按公开 run_id 取运行详情（含 cases）；黄金集被删除时仍可见。"""
        async with self._uow.read_only() as session:
            row = (
                await session.execute(
                    select(GoldenRunRow, GoldenSetRow.name, GoldenSetRow.tenant_id)
                    .outerjoin(GoldenSetRow, GoldenSetRow.id == GoldenRunRow.golden_set_id)
                    .where(GoldenRunRow.run_id == run_id)
                )
            ).first()
        if row is None:
            return None
        return self._golden_run_info(row[0], golden_set_name=row[1], tenant_id=row[2])

    async def list_golden_runs(
        self, *, limit: int = 20, golden_set_id: uuid.UUID | None = None
    ) -> list[GoldenRunInfo]:
        async with self._uow.read_only() as session:
            statement = (
                select(GoldenRunRow, GoldenSetRow.name, GoldenSetRow.tenant_id)
                # outer join：黄金集被删除的孤立运行仍可见（历史不被静默隐藏）
                .outerjoin(GoldenSetRow, GoldenSetRow.id == GoldenRunRow.golden_set_id)
                .order_by(GoldenRunRow.created_at.desc())
                .limit(limit)
            )
            if golden_set_id is not None:
                statement = statement.where(GoldenRunRow.golden_set_id == golden_set_id)
            rows = (await session.execute(statement)).all()
            return [
                self._golden_run_info(row, golden_set_name=name, tenant_id=tenant)
                for row, name, tenant in rows
            ]

    # ------------------------------------------------------------------
    # 复核
    # ------------------------------------------------------------------

    async def list_review_items(
        self,
        tenant_id: uuid.UUID,
        *,
        status: str | None,
        cursor: str | None,
        limit: int,
        reason: str | None = None,
        version_id: uuid.UUID | None = None,
    ) -> tuple[list[ReviewItemInfo], str | None, bool]:
        from owli_parser.core.pagination import decode_cursor

        conditions = [ReviewItemRow.tenant_id == tenant_id]
        if status is not None:
            conditions.append(ReviewItemRow.status == status)
        if reason is not None:
            conditions.append(ReviewItemRow.reason == reason)
        if version_id is not None:
            conditions.append(ReviewItemRow.version_id == version_id)
        offset = 0
        if cursor:
            offset = int(decode_cursor(cursor).get("offset", 0))
        async with self._uow.read_only() as session:
            rows = (
                await session.execute(
                    select(ReviewItemRow, DocVersionRow.document_id)
                    # outer join：版本被删除的孤立待办仍须可见可处置（不静默消失）
                    .outerjoin(DocVersionRow, DocVersionRow.id == ReviewItemRow.version_id)
                    .where(*conditions)
                    .order_by(ReviewItemRow.created_at, ReviewItemRow.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
            has_more = len(rows) > limit
            items = [self._review_info(row, document_id=document_id) for row, document_id in rows[:limit]]
            next_cursor = encode_cursor({"offset": offset + limit}) if has_more else None
            return items, next_cursor, has_more

    async def count_open_reviews_by_version(
        self, tenant_id: uuid.UUID, version_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """按 version 统计待处置复核数（单次聚合查询，避免 N+1）。"""
        if not version_ids:
            return {}
        async with self._uow.read_only() as session:
            rows = await session.execute(
                select(ReviewItemRow.version_id, func.count())
                .where(
                    ReviewItemRow.tenant_id == tenant_id,
                    ReviewItemRow.version_id.in_(version_ids),
                    ReviewItemRow.status == "open",
                )
                .group_by(ReviewItemRow.version_id)
            )
            return {version_id: int(count) for version_id, count in rows}

    async def resolve_review_item(
        self,
        tenant_id: uuid.UUID,
        review_item_id: uuid.UUID,
        *,
        action: str,
        note: str | None,
        reviewer_id: uuid.UUID | None,
    ) -> ReviewItemInfo:
        """处置使用 CAS（仅 open 可处置），重复处置返回 409，不静默覆盖他人结论。"""
        if action not in {"accept", "fix", "rerun_page", "switch_adapter", "dismiss"}:
            raise ValidationFailedError("不支持的处置动作")
        async with self._uow.begin() as session:
            row = (
                await session.execute(
                    select(ReviewItemRow).where(
                        ReviewItemRow.id == review_item_id,
                        ReviewItemRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("复核项不存在")
            if row.status != "open":
                raise ConflictError("复核项已被处置，请刷新后重试")
            row.status = "resolved"
            row.reviewer_id = reviewer_id
            row.resolution = {
                "action": action,
                "note": note,
                "resolved_at": self._clock.now().isoformat(),
            }
            await session.flush()
            document_id = (
                await session.execute(
                    select(DocVersionRow.document_id).where(DocVersionRow.id == row.version_id)
                )
            ).scalar_one_or_none()
            return self._review_info(row, document_id=document_id)

    async def enqueue_review_items(
        self,
        *,
        tenant_id: uuid.UUID,
        version_id: uuid.UUID,
        job_id: uuid.UUID,
        block_ids: list[str],
        reason: str,
        session: Any,
    ) -> int:
        """批量登记复核项；幂等重建：同 version+reason 的 open 项先清除。

        与 chunk 重建语义一致——任务重试重跑不会累积重复的待处置项；
        已处置（resolved）的历史记录保留不动。
        """
        from sqlalchemy import delete

        await session.execute(
            delete(ReviewItemRow).where(
                ReviewItemRow.version_id == version_id,
                ReviewItemRow.reason == reason,
                ReviewItemRow.status == "open",
            )
        )
        created = 0
        for block_id in block_ids[:200]:
            session.add(
                ReviewItemRow(
                    tenant_id=tenant_id,
                    version_id=version_id,
                    job_id=job_id,
                    block_id=block_id,
                    reason=reason,
                    status="open",
                )
            )
            created += 1
        return created

    async def _to_info(self, session: Any, row: ChunkRow) -> ChunkInfo:
        revision_row = (
            await session.execute(
                select(ChunkRevisionRow).where(
                    ChunkRevisionRow.chunk_id == row.id,
                    ChunkRevisionRow.revision == row.current_revision,
                )
            )
        ).scalar_one_or_none()
        return ChunkInfo(
            id=row.id,
            version_id=row.version_id,
            doc_id=row.doc_id,
            dataset_id=row.dataset_id,
            ordinal=row.ordinal,
            kind=row.kind,
            parent_id=row.parent_id,
            current_revision=row.current_revision,
            text=revision_row.text if revision_row else "",
            token_count=revision_row.token_count if revision_row else 0,
            source_refs=list(revision_row.source_refs or []) if revision_row else [],
            explanation=revision_row.explanation if revision_row else None,
            structure=row.structure or {},
            enabled=row.enabled,
            embedding_state=row.embedding_state,
            acl_ref=row.acl_ref,
        )

    @staticmethod
    def _manifest_info(row: IndexManifestRow) -> ManifestInfo:
        return ManifestInfo(
            id=row.id,
            version_id=row.version_id,
            index_generation=row.index_generation,
            required_channels=list(row.required_channels or []),
            expected_count=row.expected_count,
            state=row.state,
            acknowledgements=dict(row.acknowledgements or {}),
        )

    @staticmethod
    def _action_info(row: IndexActionRow) -> IndexActionInfo:
        return IndexActionInfo(
            id=row.id,
            tenant_id=row.tenant_id,
            chunk_id=row.chunk_id,
            version_id=row.version_id,
            desired_revision=row.desired_revision,
            action=row.action,
            state=row.state,
            attempts=row.attempts,
            last_error=row.last_error,
        )

    @staticmethod
    def _golden_set_info(row: GoldenSetRow) -> GoldenSetInfo:
        return GoldenSetInfo(
            id=row.id,
            tenant_id=row.tenant_id,
            name=row.name,
            cases=list(row.cases or []),
            expectations=dict(row.expectations or {}),
        )

    @staticmethod
    def _golden_run_info(
        row: GoldenRunRow,
        *,
        golden_set_name: str | None = None,
        tenant_id: uuid.UUID | None = None,
    ) -> GoldenRunInfo:
        return GoldenRunInfo(
            id=row.id,
            golden_set_id=row.golden_set_id,
            golden_set_name=golden_set_name,
            run_id=row.run_id,
            adapter_ref=row.adapter_ref,
            config_digest=row.config_digest,
            metrics=dict(row.metrics or {}),
            state=row.state,
            created_at=row.created_at,
            tenant_id=tenant_id,
        )

    @staticmethod
    def _review_info(row: ReviewItemRow, *, document_id: uuid.UUID | None = None) -> ReviewItemInfo:
        return ReviewItemInfo(
            id=row.id,
            version_id=row.version_id,
            document_id=document_id,
            job_id=row.job_id,
            block_id=row.block_id,
            reason=row.reason,
            status=row.status,
            reviewer_id=row.reviewer_id,
            resolution=row.resolution,
            created_at=row.created_at,
        )


__all__ = ["ChunkInfo", "KnowledgeService", "ManifestInfo", "ReviewItemInfo"]
