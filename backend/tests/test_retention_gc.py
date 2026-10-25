"""L-3：软删文档保留期 GC。

- 到期文档：索引按版本删除 → 行清理 → source/IR artifact 对象与登记回收；
- 共享 source artifact：仍有 doc_version 引用时保留，最后引用消失后才删除；
- 未到期不回收；重复执行幂等；
- 孤儿 artifact（从未被版本引用）按保留期回收，活文档引用的 IR artifact 不误删。
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update

from owli_parser.core.context import ActorKind
from owli_parser.core.errors import NotFoundError
from owli_parser.infrastructure.telemetry import metrics_text
from owli_parser.modules.artifacts.models import ArtifactRow
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.models import DocumentRow, DocVersionRow
from owli_parser.modules.catalog.service import CatalogScope
from owli_parser.modules.knowledge.models import (
    ChunkRevisionRow,
    ChunkRow,
    IndexManifestRow,
    ReviewItemRow,
)
from tests.conftest import create_user_with_tenant

_RETENTION_DAYS = 30


def _scope(user_id: uuid.UUID, tenant_id: uuid.UUID) -> CatalogScope:
    return CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )


async def _upload(container, scope: CatalogScope, filename: str, data: bytes) -> tuple[Any, uuid.UUID]:
    dataset = await container.catalog.create_dataset(
        scope, name=f"ds-{uuid.uuid4().hex[:8]}", description=None, visibility="tenant"
    )
    upload = await container.uploads.init_upload(
        scope, dataset_id=dataset.id, filename=filename, declared_total_bytes=len(data)
    )
    await container.uploads.put_part(scope, upload.id, 1, iter([data]))
    artifact_id = await container.uploads.complete_upload(scope, upload.id)
    return dataset, artifact_id


async def _create_parsed_document(container, username: str):
    user_id, tenant_id, _ = await create_user_with_tenant(container, username=username)
    scope = _scope(user_id, tenant_id)
    dataset, artifact_id = await _upload(container, scope, "retention.txt", b"retention sample text")
    result = await container.submit_parsing.submit(
        scope,
        artifact_id=artifact_id,
        dataset_id=dataset.id,
        pipeline_ref=None,
        document_id=None,
        expected_revision=None,
        filename="retention.txt",
    )
    await container.run_parsing_job._execute(
        result.job_id, "worker-test", container.submit_parsing.compile_default_pipeline()
    )
    return scope, tenant_id, result, artifact_id


async def _soft_delete(container, scope: CatalogScope, document_id: uuid.UUID, *, days_ago: int):
    await container.catalog.delete_document(scope, document_id)
    async with container.uow.begin() as session:
        await session.execute(
            update(DocumentRow)
            .where(DocumentRow.id == document_id)
            .values(deleted_at=container.clock.now() - timedelta(days=days_ago))
        )


async def _count(container, model, *conditions) -> int:
    async with container.uow.read_only() as session:
        return int(
            (await session.execute(select(func.count()).select_from(model).where(*conditions))).scalar_one()
        )


async def test_purges_expired_document_indexes_rows_and_artifacts(container, monkeypatch) -> None:
    scope, tenant_id, result, source_artifact_id = await _create_parsed_document(container, "gc-full")
    version_id = result.version_id
    async with container.uow.read_only() as session:
        ir_artifact_id = (
            await session.execute(select(DocVersionRow.ir_artifact_id).where(DocVersionRow.id == version_id))
        ).scalar_one()
    assert ir_artifact_id is not None

    indexed: list[uuid.UUID] = []

    async def fake_delete(*, tenant_id: uuid.UUID, version_id: uuid.UUID) -> int:
        assert tenant_id
        indexed.append(version_id)
        return 2

    monkeypatch.setattr(container.index_publication, "delete_version_indexes", fake_delete)

    await _soft_delete(container, scope, result.document_id, days_ago=_RETENTION_DAYS + 1)
    report = await container.retention.purge_deleted_documents()

    assert report.documents == 1
    assert report.versions == 1
    assert report.artifacts >= 2
    assert report.index_entries == 2
    assert report.errors == 0
    assert indexed == [version_id]

    assert await _count(container, DocumentRow, DocumentRow.id == result.document_id) == 0
    assert await _count(container, DocVersionRow, DocVersionRow.id == version_id) == 0
    assert await _count(container, ChunkRow, ChunkRow.version_id == version_id) == 0
    assert await _count(container, IndexManifestRow, IndexManifestRow.version_id == version_id) == 0
    assert await _count(container, ReviewItemRow, ReviewItemRow.version_id == version_id) == 0
    # chunk_revisions 通过 chunk 子查询删除；本容器内该版本块已无残留
    assert await _count(container, ChunkRevisionRow) == 0

    with pytest.raises(NotFoundError):
        await container.artifacts.get_artifact(tenant_id, source_artifact_id)
    with pytest.raises(NotFoundError):
        await container.artifacts.get_artifact(tenant_id, ir_artifact_id)

    assert "owli_retention_purged_documents_total" in metrics_text()

    again = await container.retention.purge_deleted_documents()
    assert again.documents == 0


async def test_shared_source_artifact_survives_until_last_reference(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="gc-shared")
    scope = _scope(user_id, tenant_id)
    dataset, source_artifact_id = await _upload(container, scope, "shared.txt", b"shared artifact")

    async def submit() -> uuid.UUID:
        result = await container.submit_parsing.submit(
            scope,
            artifact_id=source_artifact_id,
            dataset_id=dataset.id,
            pipeline_ref=None,
            document_id=None,
            expected_revision=None,
            filename="shared.txt",
        )
        return result.document_id

    doc_one = await submit()
    doc_two = await submit()
    assert (await container.artifacts.get_artifact(tenant_id, source_artifact_id)).ref_count == 2

    await _soft_delete(container, scope, doc_one, days_ago=_RETENTION_DAYS + 1)
    await container.retention.purge_deleted_documents()
    assert (await container.artifacts.get_artifact(tenant_id, source_artifact_id)).ref_count == 1

    await _soft_delete(container, scope, doc_two, days_ago=_RETENTION_DAYS + 1)
    await container.retention.purge_deleted_documents()
    with pytest.raises(NotFoundError):
        await container.artifacts.get_artifact(tenant_id, source_artifact_id)


async def test_recent_soft_delete_is_kept(container) -> None:
    scope, tenant_id, result, source_artifact_id = await _create_parsed_document(container, "gc-recent")
    await _soft_delete(container, scope, result.document_id, days_ago=1)
    report = await container.retention.purge_deleted_documents()

    assert report.documents == 0
    assert await _count(container, DocumentRow, DocumentRow.id == result.document_id) == 1
    assert (await container.artifacts.get_artifact(tenant_id, source_artifact_id)).ref_count == 1


async def test_orphan_artifacts_swept_but_referenced_ir_kept(container) -> None:
    scope, tenant_id, result, _source_artifact_id = await _create_parsed_document(container, "gc-orphan")
    async with container.uow.read_only() as session:
        ir_artifact_id = (
            await session.execute(
                select(DocVersionRow.ir_artifact_id).where(DocVersionRow.id == result.version_id)
            )
        ).scalar_one()
    # 有引用的 IR artifact（ref_count=0）即使超期也不得回收
    async with container.uow.begin() as session:
        await session.execute(
            update(ArtifactRow)
            .where(ArtifactRow.id == ir_artifact_id)
            .values(created_at=container.clock.now() - timedelta(days=_RETENTION_DAYS + 1))
        )
    # 从未被版本引用的孤儿 artifact
    _orphan_dataset, orphan_id = await _upload(container, scope, "orphan.txt", b"orphan data")
    async with container.uow.begin() as session:
        await session.execute(
            update(ArtifactRow)
            .where(ArtifactRow.id == orphan_id)
            .values(created_at=container.clock.now() - timedelta(days=_RETENTION_DAYS + 1))
        )

    report = await container.retention.purge_deleted_documents()
    assert report.documents == 0
    assert report.artifacts >= 1
    with pytest.raises(NotFoundError):
        await container.artifacts.get_artifact(tenant_id, orphan_id)
    # 活文档引用的 IR artifact 保留
    assert (await container.artifacts.get_artifact(tenant_id, ir_artifact_id)).kind == "ir"
