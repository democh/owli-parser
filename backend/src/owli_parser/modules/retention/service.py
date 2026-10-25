"""软删文档 GC：保留期后回收索引、对象与数据库行（可重入、按引用判定）。

- 只处理 deleted_at 早于保留期的文档；先删索引，再在单个事务内删行；
- artifact 只在“无任何 doc_version 引用”时回收（ref_count 可能滞后，不作唯一依据）；
- 对象删除失败保留 artifact 登记并返回错误计数，下一轮重试（不误删共享对象）；
- jobs/job_events/chat evidence_snapshots 作为审计与冻结答案保留，不做级联删除。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, exists, func, or_, select

from owli_parser.core.clock import Clock
from owli_parser.infrastructure.telemetry import METRICS
from owli_parser.infrastructure.uow import UnitOfWork
from owli_parser.modules.artifacts.models import ArtifactRow
from owli_parser.modules.artifacts.service import ArtifactService
from owli_parser.modules.catalog.models import DocumentRow, DocVersionRow
from owli_parser.modules.knowledge.models import (
    ChunkRevisionRow,
    ChunkRow,
    IndexActionRow,
    IndexManifestRow,
    ReviewItemRow,
)
from owli_parser.modules.retrieval.indexing import IndexPublicationService


@dataclass(frozen=True, slots=True)
class RetentionReport:
    documents: int = 0
    versions: int = 0
    artifacts: int = 0
    index_entries: int = 0
    errors: int = 0


class RetentionService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        *,
        retention_days: int,
        batch_size: int,
        artifacts: ArtifactService,
        index_publication: IndexPublicationService | None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._retention_days = retention_days
        self._batch_size = batch_size
        self._artifacts = artifacts
        self._index_publication = index_publication

    async def purge_deleted_documents(self, *, limit: int | None = None) -> RetentionReport:
        cutoff = self._clock.now() - timedelta(days=self._retention_days)
        batch = limit or self._batch_size
        async with self._uow.read_only() as session:
            doc_ids = (
                (
                    await session.execute(
                        select(DocumentRow.id)
                        .where(
                            DocumentRow.deleted_at.is_not(None),
                            DocumentRow.deleted_at <= cutoff,
                        )
                        .order_by(DocumentRow.deleted_at)
                        .limit(batch)
                    )
                )
                .scalars()
                .all()
            )
        totals = {"documents": 0, "versions": 0, "artifacts": 0, "index_entries": 0, "errors": 0}
        for doc_id in doc_ids:
            report = await self._purge_document(doc_id)
            totals["documents"] += report.documents
            totals["versions"] += report.versions
            totals["artifacts"] += report.artifacts
            totals["index_entries"] += report.index_entries
            totals["errors"] += report.errors
        swept, sweep_errors = await self._purge_orphan_artifacts(cutoff, batch)
        totals["artifacts"] += swept
        totals["errors"] += sweep_errors
        report = RetentionReport(**totals)
        METRICS.inc("owli_retention_purged_documents_total", amount=float(report.documents))
        METRICS.inc("owli_retention_purged_artifacts_total", amount=float(report.artifacts))
        METRICS.inc("owli_retention_index_entries_total", amount=float(report.index_entries))
        if report.errors:
            METRICS.inc("owli_retention_errors_total", amount=float(report.errors))
        return report

    async def _purge_document(self, doc_id: uuid.UUID) -> RetentionReport:
        async with self._uow.read_only() as session:
            doc = (
                await session.execute(select(DocumentRow).where(DocumentRow.id == doc_id))
            ).scalar_one_or_none()
            if doc is None or doc.deleted_at is None:
                return RetentionReport()
            tenant_id = doc.tenant_id
            versions = (
                (await session.execute(select(DocVersionRow).where(DocVersionRow.document_id == doc_id)))
                .scalars()
                .all()
            )
            version_rows = [(row.id, row.source_artifact_id, row.ir_artifact_id) for row in versions]

        index_entries = 0
        errors = 0
        if self._index_publication is not None:
            for version_id, _, _ in version_rows:
                try:
                    index_entries += await self._index_publication.delete_version_indexes(
                        tenant_id=tenant_id, version_id=version_id
                    )
                except Exception:
                    # 索引不可用不阻塞行清理：检索侧权威校验会拒绝残留旧条目
                    errors += 1

        version_ids = [row[0] for row in version_rows]
        try:
            async with self._uow.begin() as session:
                if version_ids:
                    chunk_ids = select(ChunkRow.id).where(
                        ChunkRow.version_id.in_(version_ids),
                        ChunkRow.tenant_id == tenant_id,
                    )
                    await session.execute(
                        delete(ChunkRevisionRow).where(ChunkRevisionRow.chunk_id.in_(chunk_ids))
                    )
                    await session.execute(
                        delete(IndexActionRow).where(IndexActionRow.version_id.in_(version_ids))
                    )
                    await session.execute(
                        delete(IndexManifestRow).where(IndexManifestRow.version_id.in_(version_ids))
                    )
                    await session.execute(
                        delete(ReviewItemRow).where(ReviewItemRow.version_id.in_(version_ids))
                    )
                    await session.execute(
                        delete(ChunkRow).where(
                            ChunkRow.version_id.in_(version_ids),
                            ChunkRow.tenant_id == tenant_id,
                        )
                    )
                    await session.execute(
                        delete(DocVersionRow).where(
                            DocVersionRow.id.in_(version_ids),
                            DocVersionRow.tenant_id == tenant_id,
                        )
                    )
                    for _, source_artifact_id, _ in version_rows:
                        await self._artifacts.decrement_ref(session, source_artifact_id)
                await session.execute(
                    delete(DocumentRow).where(DocumentRow.id == doc_id, DocumentRow.tenant_id == tenant_id)
                )
        except Exception:
            return RetentionReport(errors=errors + 1)

        artifact_ids = {source for _, source, _ in version_rows}
        artifact_ids |= {ir for _, _, ir in version_rows if ir is not None}
        purged = 0
        for artifact_id in artifact_ids:
            if not await self._artifact_unreferenced(artifact_id):
                continue
            if await self._artifacts.purge_artifact(tenant_id, artifact_id):
                purged += 1
            else:
                errors += 1
        return RetentionReport(
            documents=1,
            versions=len(version_rows),
            artifacts=purged,
            index_entries=index_entries,
            errors=errors,
        )

    async def _purge_orphan_artifacts(self, cutoff: datetime, batch: int) -> tuple[int, int]:
        """回收保留期前创建、且无任何 doc_version 引用的 source/ir artifact。"""
        referenced = exists().where(
            or_(
                DocVersionRow.source_artifact_id == ArtifactRow.id,
                DocVersionRow.ir_artifact_id == ArtifactRow.id,
            )
        )
        async with self._uow.read_only() as session:
            rows = (
                await session.execute(
                    select(ArtifactRow.id, ArtifactRow.tenant_id)
                    .where(
                        ArtifactRow.kind.in_(["source", "ir"]),
                        ArtifactRow.created_at < cutoff,
                        ~referenced,
                    )
                    .order_by(ArtifactRow.created_at)
                    .limit(batch)
                )
            ).all()
        purged = 0
        errors = 0
        for artifact_id, tenant_id in rows:
            if not await self._artifact_unreferenced(artifact_id):
                continue
            if await self._artifacts.purge_artifact(tenant_id, artifact_id):
                purged += 1
            else:
                errors += 1
        return purged, errors

    async def _artifact_unreferenced(self, artifact_id: uuid.UUID) -> bool:
        async with self._uow.read_only() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(DocVersionRow)
                    .where(
                        or_(
                            DocVersionRow.source_artifact_id == artifact_id,
                            DocVersionRow.ir_artifact_id == artifact_id,
                        )
                    )
                )
            ).scalar_one()
        return int(count) == 0


__all__ = ["RetentionReport", "RetentionService"]
