"""模型能力档案与用途绑定：校验、If-Match、权限、停用引用保护与解析。

三层：连接（已发布）→ 能力档案（不可变）→ 用途绑定（租户级，purpose 唯一）。
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from owli_parser.bootstrap.app import create_app
from owli_parser.core.errors import (
    ConflictError,
    NotFoundError,
    RevisionConflictError,
    ValidationFailedError,
)
from tests.conftest import create_user_with_tenant


async def _login(client: AsyncClient, username: str, tenant_id: uuid.UUID) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/login", json={"username": username, "password": "correct-horse-battery"}
    )
    assert response.status_code == 200, response.text
    return {
        "X-OWLI-CSRF": response.json()["csrf_token"],
        "X-Tenant-ID": str(tenant_id),
    }


async def _published_connection(container, tenant_id: uuid.UUID, actor_id: uuid.UUID) -> uuid.UUID:
    row = await container.configuration.create_model_connection(
        tenant_id,
        name=f"conn-{uuid.uuid4().hex[:6]}",
        protocol="openai_compatible",
        base_url="http://models.internal:8000/v1",
        vendor_label=None,
        region=None,
        credential_ref=None,
        actor_id=actor_id,
    )
    published = await container.configuration.publish_model_connection(tenant_id, row.id)
    assert published.state == "published"
    return row.id


def _profile_kwargs(**overrides):
    base = {
        "model_name": "text-embedding-3-small",
        "capabilities": ["embedding"],
        "context_window": 8192,
        "tokenizer_ref": None,
        "max_input_tokens": 8191,
        "max_output_tokens": None,
        "supported_parameters": [],
        "pricing_ref": None,
        "embedding_dimensions": 1536,
        "embedding_normalization": "l2",
        "embedding_distance": "cosine",
        "output_kinds": [],
    }
    base.update(overrides)
    return base


async def test_profile_requires_published_connection_and_dimensions(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="mp-svc")
    # 草稿连接 → 拒绝
    draft = await container.configuration.create_model_connection(
        tenant_id,
        name="conn-draft",
        protocol="openai_compatible",
        base_url="http://models.internal:8000/v1",
        vendor_label=None,
        region=None,
        credential_ref=None,
        actor_id=user_id,
    )
    try:
        await container.configuration.create_model_profile(
            tenant_id, connection_id=draft.id, actor_id=user_id, **_profile_kwargs()
        )
        raise AssertionError("草稿连接应被拒绝")
    except ValidationFailedError:
        pass

    connection_id = await _published_connection(container, tenant_id, user_id)
    # embedding 缺维度 → 拒绝（维度是索引契约）
    try:
        await container.configuration.create_model_profile(
            tenant_id,
            connection_id=connection_id,
            actor_id=user_id,
            **_profile_kwargs(embedding_dimensions=None),
        )
        raise AssertionError("缺 embedding_dimensions 应被拒绝")
    except ValidationFailedError:
        pass

    profile = await container.configuration.create_model_profile(
        tenant_id, connection_id=connection_id, actor_id=user_id, **_profile_kwargs()
    )
    assert profile.state == "published"
    assert profile.embedding_dimensions == 1536
    assert profile.capabilities == ["embedding"]

    # 跨租户查询 404
    try:
        await container.configuration.get_model_profile(uuid.uuid4(), profile.id)
        raise AssertionError("跨租户应 404")
    except NotFoundError:
        pass

    # 非法能力 400
    from owli_parser.core.errors import InvalidInputError

    try:
        await container.configuration.create_model_profile(
            tenant_id,
            connection_id=connection_id,
            actor_id=user_id,
            **_profile_kwargs(capabilities=["embedding", "telepathy"]),
        )
        raise AssertionError("非法能力应被拒绝")
    except InvalidInputError:
        pass


async def test_binding_capability_checks_and_if_match(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="mb-svc")
    connection_id = await _published_connection(container, tenant_id, user_id)
    embedding = await container.configuration.create_model_profile(
        tenant_id, connection_id=connection_id, actor_id=user_id, **_profile_kwargs()
    )
    generation = await container.configuration.create_model_profile(
        tenant_id,
        connection_id=connection_id,
        actor_id=user_id,
        **_profile_kwargs(model_name="gpt-4o-mini", capabilities=["generation"]),
    )

    # 能力不匹配（generation 用途绑定 embedding 档案）→ 拒绝
    try:
        await container.configuration.upsert_model_binding(
            tenant_id,
            "generation",
            primary_profile_id=embedding.id,
            fallback_profile_ids=[],
            expected_revision=None,
            actor_id=user_id,
        )
        raise AssertionError("能力不匹配应被拒绝")
    except ValidationFailedError:
        pass

    # 首次创建免 If-Match
    binding = await container.configuration.upsert_model_binding(
        tenant_id,
        "embedding",
        primary_profile_id=embedding.id,
        fallback_profile_ids=[],
        expected_revision=None,
        actor_id=user_id,
    )
    assert binding.revision == 1
    assert binding.primary_model_name == "text-embedding-3-small"

    # fallback 同样校验用途能力（generation 档案无 embedding 能力）→ 拒绝
    try:
        await container.configuration.upsert_model_binding(
            tenant_id,
            "embedding",
            primary_profile_id=embedding.id,
            fallback_profile_ids=[str(generation.id)],
            expected_revision=1,
            actor_id=user_id,
        )
        raise AssertionError("fallback 能力不匹配应被拒绝")
    except ValidationFailedError:
        pass

    # 更新缺 If-Match → 412
    try:
        await container.configuration.upsert_model_binding(
            tenant_id,
            "embedding",
            primary_profile_id=embedding.id,
            fallback_profile_ids=[],
            expected_revision=None,
            actor_id=user_id,
        )
        raise AssertionError("更新缺 If-Match 应被拒绝")
    except RevisionConflictError:
        pass

    updated = await container.configuration.upsert_model_binding(
        tenant_id,
        "embedding",
        primary_profile_id=embedding.id,
        fallback_profile_ids=[],
        expected_revision=1,
        actor_id=user_id,
    )
    assert updated.revision == 2

    # 停用仍被引用的档案 → 409
    try:
        await container.configuration.set_model_profile_state(tenant_id, embedding.id, state="disabled")
        raise AssertionError("仍被引用应拒绝停用")
    except ConflictError:
        pass

    # 未被引用的 generation 档案可停用 → 绑定校验拒绝 disabled
    disabled = await container.configuration.set_model_profile_state(
        tenant_id, generation.id, state="disabled"
    )
    assert disabled.state == "disabled"
    try:
        await container.configuration.upsert_model_binding(
            tenant_id,
            "generation",
            primary_profile_id=generation.id,
            fallback_profile_ids=[],
            expected_revision=None,
            actor_id=user_id,
        )
        raise AssertionError("disabled 档案应被拒绝")
    except ValidationFailedError:
        pass

    # 解析读取（供装配层使用）
    resolved = await container.configuration.get_model_binding(tenant_id, "embedding")
    assert resolved is not None and resolved.primary_model_name == "text-embedding-3-small"
    assert await container.configuration.get_model_binding(tenant_id, "rerank") is None
    bindings = await container.configuration.list_model_bindings(tenant_id)
    assert [item.purpose for item in bindings] == ["embedding"]


async def test_model_profile_api_surface(container) -> None:
    """API 端点：创建/列表/停用/绑定 upsert + If-Match 语义（415 严格校验留待契约）。"""
    username = "mp-api"
    user_id, tenant_id, _ = await create_user_with_tenant(container, username=username)
    connection_id = await _published_connection(container, tenant_id, user_id)
    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, username, tenant_id)

        created = await client.post(
            "/v1/model-profiles",
            json={
                "connection_id": str(connection_id),
                "model_name": "bge-m3",
                "capabilities": ["embedding"],
                "embedding_dimensions": 1024,
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        profile = created.json()
        assert profile["embedding_dimensions"] == 1024

        listed = await client.get("/v1/model-profiles", headers=headers)
        assert listed.status_code == 200
        assert any(item["id"] == profile["id"] for item in listed.json())

        bound = await client.put(
            "/v1/model-bindings/embedding",
            json={"primary_profile_id": profile["id"], "fallback_profile_ids": []},
            headers=headers,
        )
        assert bound.status_code == 200, bound.text
        assert bound.json()["revision"] == 1

        # 更新缺 If-Match → 412
        conflict = await client.put(
            "/v1/model-bindings/embedding",
            json={"primary_profile_id": profile["id"], "fallback_profile_ids": []},
            headers=headers,
        )
        assert conflict.status_code == 412

        updated = await client.put(
            "/v1/model-bindings/embedding",
            json={"primary_profile_id": profile["id"], "fallback_profile_ids": []},
            headers={**headers, "If-Match": '"1"'},
        )
        assert updated.status_code == 200
        assert updated.json()["revision"] == 2

        # 非法用途 → 400
        bad_purpose = await client.put(
            "/v1/model-bindings/telepathy",
            json={"primary_profile_id": profile["id"]},
            headers=headers,
        )
        assert bad_purpose.status_code == 400

        # 停用被引用档案 → 409
        disabled = await client.post(
            f"/v1/model-profiles/{profile['id']}/state",
            json={"state": "disabled"},
            headers=headers,
        )
        assert disabled.status_code == 409
