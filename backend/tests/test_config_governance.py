"""配置发布审计（config_revisions）与密钥引用登记（secret_refs）测试。

- 发布动作（模型连接/存储 profile/pipeline 保存）落不可变审计记录（digest 稳定）；
- 密钥引用登记幂等（同引用重复出现不重复建条）与轮换版本递增；
- API：列表查询（admin）与轮换端点；引用字符串可见但绝无明文值。
"""

from __future__ import annotations

import uuid

import yaml as yaml_lib
from httpx import ASGITransport, AsyncClient

from owli_parser.bootstrap.app import create_app
from owli_parser.modules.parsing import pipeline as pipeline_module
from owli_parser.ports.secrets import EnvSecretResolver
from tests.conftest import create_user_with_tenant


def _inject_secrets(container, mapping: dict[str, str]) -> None:
    """容器在启动时快照 os.environ；测试直接替换解析器（仅本用例涉及模块生效）。"""
    import os

    resolver = EnvSecretResolver(env={**os.environ, **mapping})
    container.secrets = resolver
    container.configuration._secrets = resolver


async def _login(client: AsyncClient, username: str, tenant_id: uuid.UUID) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/login", json={"username": username, "password": "correct-horse-battery"}
    )
    assert response.status_code == 200, response.text
    return {
        "X-OWLI-CSRF": response.json()["csrf_token"],
        "X-Tenant-ID": str(tenant_id),
    }


async def test_config_revisions_recorded_on_publish(container) -> None:
    _inject_secrets(
        container,
        {
            "OWLI_TEST_MODEL_KEY": "sk-plaintext-never-stored",
            "OWLI_TEST_S3_KEY": "s3-secret-value",
        },
    )
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="cg-svc")

    # 模型连接：创建（draft 不记录）→ 发布（记录一条，含 credential_configured）
    connection = await container.configuration.create_model_connection(
        tenant_id,
        name="conn-gov",
        protocol="openai_compatible",
        base_url="http://models.internal:8000/v1",
        vendor_label=None,
        region=None,
        credential_ref="env:OWLI_TEST_MODEL_KEY",
        actor_id=user_id,
    )
    before = await container.configuration.list_config_revisions(tenant_id)
    assert before == []
    await container.configuration.publish_model_connection(tenant_id, connection.id)
    revisions = await container.configuration.list_config_revisions(tenant_id)
    assert len(revisions) == 1
    entry = revisions[0]
    assert entry.kind == "model_connection"
    assert entry.resource_name == "conn-gov"
    assert entry.validated_payload["credential_configured"] is True
    assert "credential_ref" not in entry.validated_payload  # 审计载荷脱敏
    assert len(entry.payload_digest) == 64  # sha256 hex

    # 密钥引用登记：env 提供者、绑定端点、幂等（再次登记同引用不新增）
    refs = await container.configuration.list_secret_refs(tenant_id)
    assert len(refs) == 1
    assert refs[0].provider == "env"
    assert refs[0].reference == "env:OWLI_TEST_MODEL_KEY"
    assert refs[0].purpose == "model_credential"
    assert refs[0].bound_endpoint == "http://models.internal:8000/v1"
    await container.configuration.create_model_connection(
        tenant_id,
        name="conn-gov-2",
        protocol="openai_compatible",
        base_url="http://models2.internal:8000/v1",
        vendor_label=None,
        region=None,
        credential_ref="env:OWLI_TEST_MODEL_KEY",
        actor_id=user_id,
    )
    refs = await container.configuration.list_secret_refs(tenant_id)
    assert len(refs) == 1
    assert refs[0].bound_endpoint == "http://models2.internal:8000/v1"  # 刷新为新端点

    # 轮换：key_version 递增
    rotated = await container.configuration.bump_secret_ref_version(tenant_id, refs[0].id)
    assert rotated.key_version == 2
    assert rotated.state == "active"

    # pipeline 保存 → 审计记录（kind=pipeline；两版本两条）
    doc = yaml_lib.safe_load(pipeline_module.load_default_pipeline_yaml())
    doc["name"] = "gov-pipe"
    first = await container.configuration.save_pipeline(
        tenant_id,
        name="gov-pipe",
        yaml_text=yaml_lib.safe_dump(doc, allow_unicode=True),
        expected_version=None,
        actor_kind="user",
        actor_id=user_id,
    )
    doc["revision"] = 2
    await container.configuration.save_pipeline(
        tenant_id,
        name="gov-pipe",
        yaml_text=yaml_lib.safe_dump(doc, allow_unicode=True),
        expected_version=first.version,
        actor_kind="user",
        actor_id=user_id,
    )
    pipeline_revisions = await container.configuration.list_config_revisions(tenant_id, kind="pipeline")
    assert [item.revision for item in pipeline_revisions] == [2, 1]  # 最新在前

    # 存储 profile：local 发布 → 审计记录（离线环境可构造）
    local_root = container.settings.storage.local.roots[0]
    profile = await container.configuration.create_storage_profile(
        tenant_id,
        name="gov-local",
        provider="local",
        config={"root_path": local_root.path, "root_ref": local_root.id},
        actor_id=user_id,
    )
    await container.configuration.publish_storage_profile(tenant_id, profile.id)
    storage_revisions = await container.configuration.list_config_revisions(tenant_id, kind="storage_profile")
    assert len(storage_revisions) == 1
    assert storage_revisions[0].validated_payload["config"]["root_ref"] == local_root.id

    # 云端存储 profile 创建（不发布，避免离线构造）→ 仅验证密钥引用登记
    s3_profile = await container.configuration.create_storage_profile(
        tenant_id,
        name="gov-s3",
        provider="s3",
        config={
            "bucket": "b",
            "credential_ref": "env:OWLI_TEST_S3_KEY",
        },
        actor_id=user_id,
    )
    assert s3_profile.state == "draft"
    refs = await container.configuration.list_secret_refs(tenant_id)
    purposes = sorted(ref.purpose for ref in refs)
    assert purposes == ["model_credential", "storage_credential"]
    # 明文值绝不落审计载荷与引用清单
    serialized = str(
        [item.validated_payload for item in await container.configuration.list_config_revisions(tenant_id)]
    )
    assert "s3-secret-value" not in serialized


async def test_config_governance_api(container) -> None:
    _inject_secrets(container, {"OWLI_TEST_API_KEY": "api-secret-value"})
    username = "cg-api"
    user_id, tenant_id, _ = await create_user_with_tenant(container, username=username)
    await container.configuration.create_model_connection(
        tenant_id,
        name="conn-api-gov",
        protocol="openai_compatible",
        base_url="http://models.internal:8000/v1",
        vendor_label=None,
        region=None,
        credential_ref="env:OWLI_TEST_API_KEY",
        actor_id=user_id,
    )
    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, username, tenant_id)

        refs = await client.get("/v1/secret-refs", headers=headers)
        assert refs.status_code == 200
        payload = refs.json()
        assert payload[0]["reference"] == "env:OWLI_TEST_API_KEY"
        assert "api-secret-value" not in refs.text  # 明文不出现在响应中

        rotated = await client.post(f"/v1/secret-refs/{payload[0]['id']}/rotate", headers=headers)
        assert rotated.status_code == 200
        assert rotated.json()["key_version"] == 2

        revisions = await client.get("/v1/config-revisions", headers=headers)
        assert revisions.status_code == 200
        assert revisions.json() == []  # 尚未发布任何配置（draft 不记录）
