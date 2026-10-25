"""设置管理测试：存储 profile 生命周期、导入位置约束、模型连接、blocks 分页。"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from owli_parser.bootstrap.app import create_app
from owli_parser.core.context import ActorKind
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.service import CatalogScope
from tests.conftest import create_user_with_tenant


async def _client(container) -> AsyncClient:
    app = create_app(container)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _login(client: AsyncClient, username: str, tenant_id: uuid.UUID) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/login", json={"username": username, "password": "correct-horse-battery"}
    )
    assert response.status_code == 200, response.text
    return {
        "X-OWLI-CSRF": response.json()["csrf_token"],
        "X-Tenant-ID": str(tenant_id),
    }


async def _fresh_admin(container, client: AsyncClient) -> dict[str, str]:
    """创建新用户/租户并登录（无需已有数据的用例）。"""
    user_id, tenant_id, _ = await create_user_with_tenant(container)
    user = await container.identity.get_user(user_id)
    return await _login(client, user.username, tenant_id)


async def test_storage_profile_lifecycle(container) -> None:
    local_root = container.settings.storage.local.roots[0]
    async with await _client(container) as client:
        headers = await _fresh_admin(container, client)

        created = await client.post(
            "/v1/storage-profiles",
            json={
                "name": "local-archive",
                "provider": "local",
                "config": {"root_path": local_root.path, "root_ref": local_root.id},
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        profile = created.json()
        assert profile["state"] == "draft"
        assert profile["revision"] == 1
        profile_id = profile["id"]

        validated = await client.post(f"/v1/storage-profiles/{profile_id}/validate", headers=headers)
        assert validated.status_code == 200
        assert validated.json()["ok"] is True

        # 显式 IO 测试（隔离前缀写读删）；测试不改变发布状态
        tested = await client.post(
            f"/v1/storage-profiles/{profile_id}/test",
            json={"include_write": True},
            headers=headers,
        )
        assert tested.status_code == 200, tested.text
        body = tested.json()
        assert body["ok"] is True, body
        assert body["steps"]["write"].startswith("ok")
        assert body["steps"]["range_read"] == "ok"
        assert body["steps"]["cleanup"] == "ok"

        published = await client.post(f"/v1/storage-profiles/{profile_id}/publish", headers=headers)
        assert published.status_code == 200
        assert published.json()["state"] == "published"

        # 修改创建新草稿 revision，历史 revision 保留（列表只返回最新）
        updated = await client.patch(
            f"/v1/storage-profiles/{profile_id}",
            json={"config": {"root_path": local_root.path, "root_ref": local_root.id, "quota_bytes": 1024}},
            headers=headers,
        )
        assert updated.status_code == 200
        assert updated.json()["revision"] == 2
        assert updated.json()["state"] == "draft"

        listing = await client.get("/v1/storage-profiles", headers=headers)
        names = {item["name"]: item for item in listing.json()}
        assert names["local-archive"]["revision"] == 2

        # 明文密钥一律拒绝保存（只允许 env:/file: 引用）
        plain_secret = await client.post(
            "/v1/storage-profiles",
            json={
                "name": "bad-secret",
                "provider": "s3",
                "config": {"bucket": "b", "credential_ref": "env:X", "secret_key": "plaintext-abc"},
            },
            headers=headers,
        )
        assert plain_secret.status_code == 400
        assert plain_secret.json()["code"] == "invalid_input"


async def test_source_location_constraints(container) -> None:
    local_root = container.settings.storage.local.roots[0]
    user_id, tenant_id, _ = await create_user_with_tenant(container)
    scope = CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )
    dataset = await container.catalog.create_dataset(
        scope, name="ds-inbox", description=None, visibility="tenant"
    )
    async with await _client(container) as client:
        user = await container.identity.get_user(user_id)
        headers = await _login(client, user.username, tenant_id)
        # local profile 不支持对象导入位置
        local_profile = await client.post(
            "/v1/storage-profiles",
            json={
                "name": "local-for-inbox",
                "provider": "local",
                "config": {"root_path": local_root.path, "root_ref": local_root.id},
            },
            headers=headers,
        )
        local_id = local_profile.json()["id"]
        rejected = await client.post(
            "/v1/source-locations",
            json={
                "dataset_id": str(dataset.id),
                "profile_revision_id": local_id,
                "bucket": "b",
                "inbox_prefix": "inbox/docs",
            },
            headers=headers,
        )
        assert rejected.status_code == 400

        # S3 profile（不真实连接云，仅登记）+ inbox 前缀不得与受管前缀重叠
        s3_profile = await client.post(
            "/v1/storage-profiles",
            json={
                "name": "s3-source",
                "provider": "s3",
                "config": {"bucket": "test-bucket", "credential_ref": "env:OWLI_OSS_TEST_KEY"},
            },
            headers=headers,
        )
        assert s3_profile.status_code == 201, s3_profile.text
        overlap = await client.post(
            "/v1/source-locations",
            json={
                "dataset_id": str(dataset.id),
                "profile_revision_id": s3_profile.json()["id"],
                "bucket": "test-bucket",
                "inbox_prefix": "tenants/something",
            },
            headers=headers,
        )
        assert overlap.status_code == 400

        ok = await client.post(
            "/v1/source-locations",
            json={
                "dataset_id": str(dataset.id),
                "profile_revision_id": s3_profile.json()["id"],
                "bucket": "test-bucket",
                "inbox_prefix": "inbox/docs",
            },
            headers=headers,
        )
        assert ok.status_code == 201, ok.text
        location_id = ok.json()["id"]
        toggled = await client.patch(
            f"/v1/source-locations/{location_id}", json={"enabled": False}, headers=headers
        )
        assert toggled.status_code == 200
        assert toggled.json()["enabled"] is False


async def test_model_connection_lifecycle(container) -> None:
    async with await _client(container) as client:
        headers = await _fresh_admin(container, client)

        created = await client.post(
            "/v1/model-connections",
            json={
                "name": "local-vllm",
                "protocol": "openai_compatible",
                "base_url": "http://127.0.0.1:8100/v1",
                "vendor_label": "vLLM",
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        connection = created.json()
        assert connection["state"] == "draft"
        assert connection["credential_configured"] is False
        connection_id = connection["id"]

        # 未配置的 credential_ref 拒绝（write-only 且存在性校验）
        missing_cred = await client.post(
            "/v1/model-connections",
            json={
                "name": "cloud-llm",
                "protocol": "openai_compatible",
                "base_url": "https://api.example.internal/v1",
                "credential_ref": "env:OWLI_DEFINITELY_MISSING_KEY",
            },
            headers=headers,
        )
        assert missing_cred.status_code == 422

        validated = await client.post(f"/v1/model-connections/{connection_id}/validate", headers=headers)
        assert validated.status_code == 200
        assert validated.json()["ok"] is True
        # offline 模式下本地端点（localhost/127.0.0.1）无需额外批准
        assert validated.json()["warnings"] == []

        published = await client.post(f"/v1/model-connections/{connection_id}/publish", headers=headers)
        assert published.status_code == 200
        assert published.json()["state"] == "published"

        # 修改创建新 revision（draft），base_url 非法直接 400
        patched = await client.patch(
            f"/v1/model-connections/{connection_id}",
            json={"vendor_label": "vLLM-local"},
            headers=headers,
        )
        assert patched.status_code == 200
        assert patched.json()["revision"] == 2
        assert patched.json()["state"] == "draft"

        bad_url = await client.patch(
            f"/v1/model-connections/{connection_id}",
            json={"base_url": "ftp://invalid"},
            headers=headers,
        )
        assert bad_url.status_code == 400


async def test_blocks_pagination(container) -> None:
    """解析闭环后按 type 过滤与 cursor 翻页读取 blocks。"""
    user_id, tenant_id, _ = await create_user_with_tenant(container)
    user = await container.identity.get_user(user_id)
    scope = CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )
    dataset = await container.catalog.create_dataset(
        scope, name="ds-blocks", description=None, visibility="tenant"
    )
    # 构造多段落 Markdown，产生 heading + 多个 text block
    body = ("# 标题一\n\n第一段内容。\n\n第二段内容。\n\n# 标题二\n\n第三段内容。\n\n第四段内容。").encode()
    upload = await container.uploads.init_upload(
        scope, dataset_id=dataset.id, filename="doc.md", declared_total_bytes=len(body)
    )
    await container.uploads.put_part(scope, upload.id, 1, iter([body]))
    artifact_id = await container.uploads.complete_upload(scope, upload.id)
    result = await container.submit_parsing.submit(
        scope,
        artifact_id=artifact_id,
        dataset_id=dataset.id,
        pipeline_ref=None,
        document_id=None,
        expected_revision=None,
        filename="doc.md",
    )
    pipeline = container.submit_parsing.compile_default_pipeline()
    await container.run_parsing_job._execute(result.job_id, "worker-test", pipeline)

    async with await _client(container) as client:
        user = await container.identity.get_user(user_id)
        headers = await _login(client, user.username, tenant_id)
        full = await client.get(f"/v1/documents/{result.document_id}/blocks?limit=100", headers=headers)
        assert full.status_code == 200, full.text
        payload = full.json()
        assert payload["total"] >= 4
        types = {item["type"] for item in payload["items"]}
        assert "heading" in types and "text" in types
        # 每个 block 至少一个来源锚点（IR 不变量）
        assert all(item["source_refs"] for item in payload["items"])

        headings = await client.get(
            f"/v1/documents/{result.document_id}/blocks?type=heading", headers=headers
        )
        assert headings.status_code == 200
        assert all(item["type"] == "heading" for item in headings.json()["items"])

        # cursor 翻页：两页合并等于全量（数量守恒且无重复）
        page1 = await client.get(f"/v1/documents/{result.document_id}/blocks?limit=2", headers=headers)
        first_page = page1.json()
        assert first_page["has_more"] is True
        page2 = await client.get(
            f"/v1/documents/{result.document_id}/blocks?limit=2&cursor={first_page['next_cursor']}",
            headers=headers,
        )
        second_page = page2.json()
        ids = [i["id"] for i in first_page["items"]] + [i["id"] for i in second_page["items"]]
        assert len(ids) == len(set(ids))


async def test_document_version_selector(container) -> None:
    """版本分页与显式 version_id 读取。"""
    user_id, tenant_id, _ = await create_user_with_tenant(container)
    user = await container.identity.get_user(user_id)
    scope = CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )
    dataset = await container.catalog.create_dataset(
        scope, name="ds-versions", description=None, visibility="tenant"
    )
    body = b"hello version one\n"
    upload = await container.uploads.init_upload(
        scope, dataset_id=dataset.id, filename="v.txt", declared_total_bytes=len(body)
    )
    await container.uploads.put_part(scope, upload.id, 1, iter([body]))
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

    versions = await container.catalog.list_versions(scope, result.document_id)
    assert len(versions) == 1
    assert versions[0].lifecycle == "published"

    async with await _client(container) as client:
        user = await container.identity.get_user(user_id)
        headers = await _login(client, user.username, tenant_id)
        by_version = await client.get(
            f"/v1/documents/{result.document_id}/chunks?version_id={versions[0].id}",
            headers=headers,
        )
        assert by_version.status_code == 200
        assert by_version.json()["items"]

        missing_version = await client.get(
            f"/v1/documents/{result.document_id}/blocks?version_id={uuid.uuid4()}",
            headers=headers,
        )
        assert missing_version.status_code == 404
