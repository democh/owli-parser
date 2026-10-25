"""纵向闭环测试：登录 → 数据集 → 上传 → 解析 → 分块 → 发布 → 权限隔离。"""

from __future__ import annotations

import uuid

import pytest

from owli_parser.core.context import ActorKind
from owli_parser.core.errors import ForbiddenError, NotFoundError
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.service import CatalogScope
from tests.conftest import create_user_with_tenant


def _scope(tenant_id: uuid.UUID, user_id: uuid.UUID, role: Role = Role.ADMIN) -> CatalogScope:
    return CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=role),
    )


async def test_login_session_and_csrf(container) -> None:
    user_id, _tenant_id, _ = await create_user_with_tenant(container)
    user = await container.identity.get_user(user_id)
    result = await container.identity.login(user.username, "correct-horse-battery")
    assert result.user.id == user_id

    session = await container.identity.authenticate_session(result.session_token)
    assert session.user.id == user_id

    # CSRF：正确 token 通过，错误 token 拒绝
    container.identity.verify_csrf(session.csrf_digest, result.csrf_token)
    with pytest.raises(ForbiddenError):
        container.identity.verify_csrf(session.csrf_digest, "wrong-token")

    # 登出后可撤销（幂等）
    await container.identity.logout(result.session_token)
    from owli_parser.core.errors import NotAuthenticatedError

    with pytest.raises(NotAuthenticatedError):
        await container.identity.authenticate_session(result.session_token)
    await container.identity.logout(result.session_token)


async def test_login_rate_limit(container) -> None:
    user_id, _, _ = await create_user_with_tenant(container)
    user = await container.identity.get_user(user_id)
    from owli_parser.core.errors import NotAuthenticatedError, RateLimitedError

    for _ in range(container.settings.auth.login_rate_limit.max_attempts):
        container.clock.advance(1)  # type: ignore[attr-defined]
        with pytest.raises(NotAuthenticatedError):
            await container.identity.login(user.username, "wrong-password")
    with pytest.raises(RateLimitedError):
        await container.identity.login(user.username, "wrong-password")


async def test_upload_parse_publish_flow(container) -> None:
    """TXT 上传 → 提交解析 → worker 执行 → 版本发布 → 分块可检索元数据。"""
    from owli_parser.bootstrap.worker import get_worker_container  # noqa: F401 - 不启动 worker

    user_id, tenant_id, _ = await create_user_with_tenant(container)
    scope = _scope(tenant_id, user_id)

    dataset = await container.catalog.create_dataset(
        scope, name="ds-1", description=None, visibility="tenant"
    )

    # 上传（两分片，共 24 字节）
    upload = await container.uploads.init_upload(
        scope, dataset_id=dataset.id, filename="note.txt", declared_total_bytes=24
    )
    await container.uploads.put_part(scope, upload.id, 1, iter([b"hello world\n"]))
    await container.uploads.put_part(scope, upload.id, 2, iter([b"second page\n"]))
    artifact_id = await container.uploads.complete_upload(scope, upload.id)

    artifact = await container.artifacts.get_artifact(tenant_id, artifact_id)
    assert artifact.kind == "source"
    assert artifact.size_bytes == 24

    # 提交解析任务
    result = await container.submit_parsing.submit(
        scope,
        artifact_id=artifact_id,
        dataset_id=dataset.id,
        pipeline_ref=None,
        document_id=None,
        expected_revision=None,
    )

    # worker 执行（直接调用编排，跳过 broker）
    pipeline = container.submit_parsing.compile_default_pipeline()
    await container.run_parsing_job._execute(result.job_id, "worker-test", pipeline)

    job = await container.jobs.get_job(tenant_id, result.job_id)
    assert job.state == "DONE", job.error
    assert job.retrieval_ready is False  # parse-only：不假报已索引

    # 事件流：seq 单调且含终态
    events, _latest = await container.jobs.list_events(tenant_id, result.job_id, after_seq=0)
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs)
    assert events[-1].to_state == "DONE"

    # 可见版本已发布；chunks 已生成且带来源
    doc = await container.catalog.get_document(scope, result.document_id)
    assert doc.visible_version_id == result.version_id

    chunks, _, _ = await container.knowledge.list_chunks(
        tenant_id, document_id=result.document_id, version_id=None, cursor=None, limit=50
    )
    assert len(chunks) >= 1
    child = next(c for c in chunks if c.kind == "child")
    assert child.text.strip()
    assert len(child.source_refs) >= 1
    assert any(ref.get("artifact_id") for ref in child.source_refs)

    # 文档读取权限继承数据集
    other_user_id, other_tenant_id, _ = await create_user_with_tenant(container)
    other_scope = _scope(other_tenant_id, other_user_id)
    with pytest.raises(NotFoundError):
        await container.catalog.get_document(other_scope, result.document_id)


async def test_chunk_edit_creates_revision(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container)
    scope = _scope(tenant_id, user_id)
    dataset = await container.catalog.create_dataset(
        scope, name="ds-2", description=None, visibility="tenant"
    )
    upload = await container.uploads.init_upload(
        scope, dataset_id=dataset.id, filename="a.txt", declared_total_bytes=12
    )
    await container.uploads.put_part(scope, upload.id, 1, iter([b"hello world\n"]))
    artifact_id = await container.uploads.complete_upload(scope, upload.id)
    result = await container.submit_parsing.submit(
        scope,
        artifact_id=artifact_id,
        dataset_id=dataset.id,
        pipeline_ref=None,
        document_id=None,
        expected_revision=None,
    )
    pipeline = container.submit_parsing.compile_default_pipeline()
    await container.run_parsing_job._execute(result.job_id, "worker-test", pipeline)

    chunks, _, _ = await container.knowledge.list_chunks(
        tenant_id, document_id=result.document_id, version_id=None, cursor=None, limit=10
    )
    chunk = chunks[0]
    edited = await container.knowledge.edit_chunk(
        tenant_id,
        chunk.id,
        expected_revision=chunk.current_revision,
        text="人工修订后的文本",
        enabled=None,
        created_by_kind="user",
        created_by_id=user_id,
    )
    assert edited.current_revision == chunk.current_revision + 1
    assert edited.text == "人工修订后的文本"
    assert edited.embedding_state == "pending"  # 正文变化后旧向量不得沿用

    # 过期 revision 变更被拒绝
    from owli_parser.core.errors import RevisionConflictError

    with pytest.raises(RevisionConflictError):
        await container.knowledge.edit_chunk(
            tenant_id,
            chunk.id,
            expected_revision=chunk.current_revision,
            text="再次修改",
            enabled=None,
            created_by_kind="user",
            created_by_id=user_id,
        )


async def test_cross_tenant_isolation(container, seeded) -> None:
    (user_a, tenant_a, _), (user_b, tenant_b, _) = seeded["a"], seeded["b"]
    scope_a = _scope(tenant_a, user_a)
    scope_b = _scope(tenant_b, user_b)

    ds_a = await container.catalog.create_dataset(scope_a, name="ds-a", description=None, visibility="tenant")
    ds_b = await container.catalog.create_dataset(scope_b, name="ds-b", description=None, visibility="tenant")

    # 租户 B 不能读取租户 A 的数据集
    with pytest.raises(NotFoundError):
        await container.catalog.get_dataset(scope_b, ds_a.id)
    # 各自列表互不可见
    items_a, _, _ = await container.catalog.list_datasets(scope_a, cursor=None, limit=50)
    items_b, _, _ = await container.catalog.list_datasets(scope_b, cursor=None, limit=50)
    assert {d.id for d in items_a} == {ds_a.id}
    assert {d.id for d in items_b} == {ds_b.id}

    # private 数据集对同租户 editor 不可见
    private_ds = await container.catalog.create_dataset(
        scope_a, name="ds-private", description=None, visibility="private"
    )
    editor_user = await container.identity.create_user(
        username=f"editor-{uuid.uuid4().hex[:8]}",
        password="correct-horse-battery",
        display_name="editor",
    )
    await container.identity.add_member(tenant_a, editor_user.username, "editor")
    editor_scope = _scope(tenant_a, editor_user.id, Role.EDITOR)
    with pytest.raises(NotFoundError):
        await container.catalog.get_dataset(editor_scope, private_ds.id)
    # tenant 可见性对 editor 可读
    got = await container.catalog.get_dataset(editor_scope, ds_a.id)
    assert got.id == ds_a.id
