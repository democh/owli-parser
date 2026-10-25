"""索引发布闭环测试：发布 → 通道写入 → manifest 确认 → retrieval_ready。

覆盖：
- enabled=true 时解析发布真实写入 lexical 通道并经 PG 权威校验可检出；
- manifest 确认与冻结集合 digest 一致（并发 revision 漂移 → 确认被拒）；
- 编辑后旧索引条目由权威校验剔除（不召回旧内容），reembed 重建单块；
- 配置要求 dense 而无 embedding 绑定时显式失败（retrieval_ready=false）。
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from owli_parser.bootstrap.app import create_app
from owli_parser.core.context import ActorKind
from owli_parser.core.errors import ConflictError
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.service import CatalogScope
from tests.conftest import create_user_with_tenant

BODY = b"hello retrieval world\n"  # 22 字节


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


async def _setup_document(
    container, username: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """启用检索后走完整解析链路。

    返回 (user, tenant, dataset, document, version, job)。
    """
    user_id, tenant_id, _ = await create_user_with_tenant(container, username=username)
    scope = _scope(tenant_id, user_id)
    dataset = await container.catalog.create_dataset(
        scope, name=f"ds-idx-{username}", description=None, visibility="tenant"
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
    job = await container.jobs.get_job(tenant_id, result.job_id)
    return user_id, tenant_id, dataset.id, result.document_id, result.version_id, job.id


async def _first_enabled_chunk(container, tenant_id: uuid.UUID, document_id: uuid.UUID):
    chunks, _, _ = await container.knowledge.list_chunks(
        tenant_id, document_id=document_id, version_id=None, cursor=None, limit=50
    )
    return next(c for c in chunks if c.enabled and c.text.strip())


async def test_publish_flow_indexes_and_sets_retrieval_ready(container) -> None:
    container.settings.retrieval.enabled = True
    _user_id, tenant_id, dataset_id, document_id, version_id, job_id = await _setup_document(
        container, "idx-admin"
    )

    # 任务终态：DONE 且可检索（索引已确认，不假报）
    job = await container.jobs.get_job(tenant_id, job_id)
    assert job.state == "DONE", job.error
    assert job.retrieval_ready is True

    # manifest：冻结集合已确认 lexical，state=ready
    manifest = await container.knowledge.latest_manifest(version_id)
    assert manifest is not None
    assert manifest.required_channels == ["lexical"]
    assert manifest.state == "ready"
    assert "lexical" in manifest.acknowledgements
    assert manifest.expected_count >= 1

    # 写入内容经 API 检索可命中（PG 权威校验通过），payload 携带文档锚点
    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, "idx-admin", tenant_id)
        response = await client.post(
            "/v1/retrieval/test",
            json={"query": "world", "dataset_ids": [str(dataset_id)], "top_k": 5},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["channels_used"] == ["lexical"]
        assert len(payload["items"]) >= 1
        assert payload["items"][0]["payload"]["document_id"] == str(document_id)


async def test_reembed_tracks_authoritative_revision(container) -> None:
    container.settings.retrieval.enabled = True
    user_id, tenant_id, dataset_id, document_id, _version_id, _job_id = await _setup_document(
        container, "idx-editor"
    )
    chunk = await _first_enabled_chunk(container, tenant_id, document_id)

    # 编辑正文 → revision 漂移；被编辑 chunk 的旧索引条目被权威校验剔除
    # （其他未编辑 chunk（如父块）仍可正常命中，不误伤）
    edited = await container.knowledge.edit_chunk(
        tenant_id,
        chunk.id,
        expected_revision=chunk.current_revision,
        text="量子计算导论",
        enabled=None,
        created_by_kind="user",
        created_by_id=user_id,
    )
    assert edited.current_revision == chunk.current_revision + 1

    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, "idx-editor", tenant_id)

        stale = await client.post(
            "/v1/retrieval/test",
            json={"query": "retrieval", "dataset_ids": [str(dataset_id)], "top_k": 5},
            headers=headers,
        )
        assert stale.status_code == 200, stale.text
        stale_payload = stale.json()
        assert all(item["payload"]["chunk_id"] != str(chunk.id) for item in stale_payload["items"]), (
            "被编辑 chunk 的旧 revision 不得进入结果"
        )
        assert any("未通过权威校验" in w for w in stale_payload["warnings"])

        reembed = await client.post(f"/v1/chunks/{chunk.id}/reembed", headers=headers)
        assert reembed.status_code == 200, reembed.text
        assert reembed.json()["revision"] == edited.current_revision
        assert reembed.json()["channels"] == ["lexical"]

        fresh = await client.post(
            "/v1/retrieval/test",
            json={"query": "量子", "dataset_ids": [str(dataset_id)], "top_k": 5},
            headers=headers,
        )
        assert fresh.status_code == 200, fresh.text
        assert any(item["payload"]["chunk_id"] == str(chunk.id) for item in fresh.json()["items"])

        # 跨租户 reembed 一律 404（不泄露存在性）
        other_name = "idx-other"
        _, other_tenant_id, _ = await create_user_with_tenant(container, username=other_name)
        other_headers = await _login(client, other_name, other_tenant_id)
        cross = await client.post(f"/v1/chunks/{chunk.id}/reembed", headers=other_headers)
        assert cross.status_code == 404


async def test_manifest_confirmation_rejects_revision_drift(container) -> None:
    container.settings.retrieval.enabled = True
    user_id, tenant_id, _dataset_id, document_id, version_id, _job_id = await _setup_document(
        container, "idx-drift"
    )
    chunk = await _first_enabled_chunk(container, tenant_id, document_id)

    # 冻结清单后发生并发编辑（revision 漂移）→ 确认必须被拒绝
    manifest = await container.knowledge.create_manifest(
        tenant_id=tenant_id,
        version_id=version_id,
        index_generation=2,
        required_channels=["lexical"],
    )
    await container.knowledge.edit_chunk(
        tenant_id,
        chunk.id,
        expected_revision=chunk.current_revision,
        text="并发编辑导致 revision 漂移",
        enabled=None,
        created_by_kind="user",
        created_by_id=user_id,
    )
    try:
        await container.index_publication.publish_version(
            tenant_id=tenant_id, version_id=version_id, generation=2, manifest=manifest
        )
        raise AssertionError("revision 漂移时通道确认必须被拒绝")
    except ConflictError:
        pass


async def test_dense_channel_without_binding_fails_explicitly(container) -> None:
    container.settings.retrieval.enabled = True
    container.settings.retrieval.channels = ["dense"]

    _user_id, tenant_id, _dataset_id, _document_id, _version_id, job_id = await _setup_document(
        container, "idx-dense"
    )
    # 任务必须显式失败且不得假报 retrieval_ready
    job = await container.jobs.get_job(tenant_id, job_id)
    assert job.state == "FAILED"
    assert job.retrieval_ready is False
    assert job.error is not None
    assert job.error.get("code") == "capability_not_ready"
