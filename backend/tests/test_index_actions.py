"""编辑 → 重索引行动闭环测试（登记 → outbox 派发 → worker 消费）。

覆盖文档要求（PATCH /v1/chunks/{id}）："后端自动登记重索引，不依赖前端再调用
reembed"；消费前旧索引条目由权威校验剔除（不召回旧内容）；失败重试环与
attempts 超限终态；手动 reembed 与自动消费共用完成路径。
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from owli_parser.bootstrap.app import create_app
from owli_parser.core.context import ActorKind
from owli_parser.infrastructure.crosscut_models import OutboxEventRow
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.service import CatalogScope
from owli_parser.modules.knowledge.models import IndexActionRow
from tests.conftest import create_user_with_tenant

BODY = b"hello retrieval world\n"


def _scope(tenant_id: uuid.UUID, user_id: uuid.UUID) -> CatalogScope:
    return CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )


async def _login(client: AsyncClient, username: str, tenant_id: uuid.UUID) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/login", json={"username": username, "password": "correct-horse-battery"}
    )
    assert response.status_code == 200, response.text
    return {
        "X-OWLI-CSRF": response.json()["csrf_token"],
        "X-Tenant-ID": str(tenant_id),
    }


async def _setup_indexed_document(container, username: str):
    """启用检索 → 完整解析发布 → 返回 (user, tenant, dataset, document, chunk)。"""
    user_id, tenant_id, _ = await create_user_with_tenant(container, username=username)
    scope = _scope(tenant_id, user_id)
    dataset = await container.catalog.create_dataset(
        scope, name=f"ds-act-{username}", description=None, visibility="tenant"
    )
    upload = await container.uploads.init_upload(
        scope, dataset_id=dataset.id, filename="note.txt", declared_total_bytes=len(BODY)
    )
    await container.uploads.put_part(scope, upload.id, 1, iter([BODY]))
    artifact_id = await container.uploads.complete_upload(scope, upload.id)
    result = await container.submit_parsing.submit(
        scope,
        artifact_id=artifact_id,
        dataset_id=dataset.id,
        pipeline_ref=None,
        document_id=None,
        expected_revision=None,
        filename="note.txt",
    )
    pipeline = container.submit_parsing.compile_default_pipeline()
    await container.run_parsing_job._execute(result.job_id, "worker-test", pipeline)
    chunks, _, _ = await container.knowledge.list_chunks(
        tenant_id, document_id=result.document_id, version_id=None, cursor=None, limit=50
    )
    chunk = next(c for c in chunks if c.enabled and c.text.strip())
    return user_id, tenant_id, dataset.id, result.document_id, chunk


async def _pending_actions(container, tenant_id: uuid.UUID) -> list[IndexActionRow]:
    async with container.uow.read_only() as session:
        return list(
            (
                await session.execute(
                    select(IndexActionRow).where(
                        IndexActionRow.tenant_id == tenant_id, IndexActionRow.state == "pending"
                    )
                )
            )
            .scalars()
            .all()
        )


async def _edit(container, tenant_id, user_id, chunk, text: str | None, enabled: bool | None):
    return await container.knowledge.edit_chunk(
        tenant_id,
        chunk.id,
        expected_revision=chunk.current_revision,
        text=text,
        enabled=enabled,
        created_by_kind="user",
        created_by_id=user_id,
    )


async def test_edit_registers_action_and_outbox_event(container) -> None:
    container.settings.retrieval.enabled = True
    user_id, tenant_id, _dataset_id, _document_id, chunk = await _setup_indexed_document(
        container, "act-admin"
    )
    edited = await _edit(container, tenant_id, user_id, chunk, "量子计算导论", None)

    actions = await _pending_actions(container, tenant_id)
    assert len(actions) == 1
    action = actions[0]
    assert action.chunk_id == chunk.id
    assert action.desired_revision == edited.current_revision
    assert action.action == "upsert"

    # 与编辑同事务登记 outbox（index.apply），供 scheduler 派发
    async with container.uow.read_only() as session:
        outbox_rows = (
            (await session.execute(select(OutboxEventRow).where(OutboxEventRow.event_type == "index.apply")))
            .scalars()
            .all()
        )
    assert len(outbox_rows) == 1
    assert outbox_rows[0].delivery_state == "pending"
    assert outbox_rows[0].payload["action_id"] == str(action.id)

    # 仅启用状态变化（文本不变）不登记行动：权威层即时生效，无需重建
    await _edit(container, tenant_id, user_id, edited, None, False)
    assert len(await _pending_actions(container, tenant_id)) == 1


async def test_scheduler_dispatches_index_action(container) -> None:
    container.settings.retrieval.enabled = True
    user_id, tenant_id, _dataset_id, _document_id, chunk = await _setup_indexed_document(
        container, "act-sched"
    )
    await _edit(container, tenant_id, user_id, chunk, "调度器派发", None)
    actions = await _pending_actions(container, tenant_id)
    assert len(actions) == 1

    from owli_parser.bootstrap.scheduler import Scheduler

    scheduler = Scheduler(container)
    scheduler.dispatcher = container.dispatcher  # type: ignore[attr-defined]
    dispatched = await scheduler._dispatch_outbox(batch=50)
    assert dispatched >= 1
    recorder = container.dispatcher  # type: ignore[attr-defined]
    assert str(actions[0].id) in recorder.index_actions

    async with container.uow.read_only() as session:
        row = (
            await session.execute(select(OutboxEventRow).where(OutboxEventRow.event_type == "index.apply"))
        ).scalar_one()
    assert row.delivery_state == "delivered"


async def test_apply_action_reindexes_and_search_hits_new_text(container) -> None:
    container.settings.retrieval.enabled = True
    user_id, tenant_id, dataset_id, _document_id, chunk = await _setup_indexed_document(
        container, "act-apply"
    )
    await _edit(container, tenant_id, user_id, chunk, "量子计算导论", None)
    action = (await _pending_actions(container, tenant_id))[0]

    # worker 消费（幂等）
    assert await container.index_publication.apply_action(action.id) == "done"
    assert await container.index_publication.apply_action(action.id) == "skipped"

    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, "act-apply", tenant_id)
        fresh = await client.post(
            "/v1/retrieval/test",
            json={"query": "量子", "dataset_ids": [str(dataset_id)], "top_k": 5},
            headers=headers,
        )
        assert fresh.status_code == 200, fresh.text
        assert any(item["payload"]["chunk_id"] == str(chunk.id) for item in fresh.json()["items"])
        # 旧正文（revision 已漂移）不得命中
        stale = await client.post(
            "/v1/retrieval/test",
            json={"query": "retrieval", "dataset_ids": [str(dataset_id)], "top_k": 5},
            headers=headers,
        )
        assert stale.status_code == 200, stale.text
        assert all(item["payload"]["chunk_id"] != str(chunk.id) for item in stale.json()["items"])


async def test_apply_action_retries_then_fails(container, monkeypatch) -> None:
    container.settings.retrieval.enabled = True
    user_id, tenant_id, _dataset_id, _document_id, chunk = await _setup_indexed_document(
        container, "act-retry"
    )
    await _edit(container, tenant_id, user_id, chunk, "失败重试内容", None)
    action = (await _pending_actions(container, tenant_id))[0]

    def boom(_documents):
        raise RuntimeError("lexical upsert 故障注入")

    monkeypatch.setattr(container.lexical_index, "upsert", boom)

    # 第 1 次失败：attempts=1，保持 pending 并重新登记 outbox 重派
    assert await container.index_publication.apply_action(action.id) == "pending"
    info = await container.knowledge.get_index_action(action.id)
    assert info is not None and info.attempts == 1 and info.last_error == "RuntimeError"
    async with container.uow.read_only() as session:
        retry_rows = (
            (
                await session.execute(
                    select(OutboxEventRow).where(
                        OutboxEventRow.event_type == "index.apply",
                        OutboxEventRow.delivery_state == "pending",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(retry_rows) == 2, "失败后应重新登记 outbox 重派（初次登记 + 重试各一条）"

    # 第 2、3 次失败：attempts 超限 → failed 终态（可手动 reembed）
    assert await container.index_publication.apply_action(action.id) == "pending"
    assert await container.index_publication.apply_action(action.id) == "failed"
    info = await container.knowledge.get_index_action(action.id)
    assert info is not None and info.attempts == 3 and info.state == "failed"
    assert await container.index_publication.apply_action(action.id) == "skipped"


async def test_reembed_resolves_pending_action(container) -> None:
    container.settings.retrieval.enabled = True
    user_id, tenant_id, _dataset_id, _document_id, chunk = await _setup_indexed_document(
        container, "act-reembed"
    )
    await _edit(container, tenant_id, user_id, chunk, "手动重建路径", None)
    action = (await _pending_actions(container, tenant_id))[0]

    result = await container.index_publication.reindex_chunk(tenant_id=tenant_id, chunk_id=chunk.id)
    assert result["resolved_actions"] == 1
    info = await container.knowledge.get_index_action(action.id)
    assert info is not None and info.state == "done"
    # 已完成行动不再被消费
    assert await container.index_publication.apply_action(action.id) == "skipped"


async def test_disabled_chunk_action_completes_without_rebuild(container) -> None:
    container.settings.retrieval.enabled = True
    user_id, tenant_id, _dataset_id, _document_id, chunk = await _setup_indexed_document(
        container, "act-disabled"
    )
    # 同一编辑同时改正文并禁用：登记行动，但消费时无需重建（权威层已不可召回）
    await _edit(container, tenant_id, user_id, chunk, "禁用同时改文", False)
    action = (await _pending_actions(container, tenant_id))[0]

    assert await container.index_publication.apply_action(action.id) == "skipped"
    info = await container.knowledge.get_index_action(action.id)
    assert info is not None and info.state == "done"
