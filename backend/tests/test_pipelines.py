"""解析管道配置测试：版本化保存（If-Match）、校验、权限、提交消费与快照执行。

覆盖文档 API `GET/PUT /v1/pipelines/{name}`（版本化配置；PUT 要求 If-Match）及
执行一致性：提交时把 pipeline YAML 冻结进任务快照，worker 只按快照执行。
"""

from __future__ import annotations

import uuid

import yaml as yaml_lib
from httpx import ASGITransport, AsyncClient

from owli_parser.bootstrap.app import create_app
from owli_parser.core.context import ActorKind
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.service import CatalogScope
from owli_parser.modules.parsing import pipeline as pipeline_module
from tests.conftest import create_user_with_tenant


def _custom_pipeline_yaml(name: str, revision: int = 1) -> str:
    doc = yaml_lib.safe_load(pipeline_module.load_default_pipeline_yaml())
    doc["name"] = name
    doc["revision"] = revision
    return yaml_lib.safe_dump(doc, allow_unicode=True, sort_keys=False)


async def _login(client: AsyncClient, username: str, tenant_id: uuid.UUID) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/login", json={"username": username, "password": "correct-horse-battery"}
    )
    assert response.status_code == 200, response.text
    return {
        "X-OWLI-CSRF": response.json()["csrf_token"],
        "X-Tenant-ID": str(tenant_id),
    }


def _scope(tenant_id: uuid.UUID, user_id: uuid.UUID) -> CatalogScope:
    return CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )


async def test_pipeline_versioned_save_with_if_match(container) -> None:
    """创建（免 If-Match）→ 更新须 If-Match → 版本递增且旧版失活 → 校验与权限。"""
    username = "pipe-admin"
    _user_id, tenant_id, _ = await create_user_with_tenant(container, username=username)
    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, username, tenant_id)

        created = await client.put(
            "/v1/pipelines/custom-fast",
            json={"yaml_text": _custom_pipeline_yaml("custom-fast")},
            headers=headers,
        )
        assert created.status_code == 200, created.text
        assert created.json()["version"] == 1

        # 更新缺 If-Match → 412（防并发覆盖）
        no_match = await client.put(
            "/v1/pipelines/custom-fast",
            json={"yaml_text": _custom_pipeline_yaml("custom-fast", revision=2)},
            headers=headers,
        )
        assert no_match.status_code == 412

        # 过期 If-Match → 412；正确 → version=2 且旧版失活
        stale = await client.put(
            "/v1/pipelines/custom-fast",
            json={"yaml_text": _custom_pipeline_yaml("custom-fast", revision=3)},
            headers={**headers, "If-Match": "99"},
        )
        assert stale.status_code == 412
        updated = await client.put(
            "/v1/pipelines/custom-fast",
            json={"yaml_text": _custom_pipeline_yaml("custom-fast", revision=2)},
            headers={**headers, "If-Match": "1"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["version"] == 2

        # 列表：租户配置覆盖/并列内置（source 区分）
        listing = await client.get("/v1/pipelines", headers=headers)
        assert listing.status_code == 200, listing.text
        items = {item["name"]: item for item in listing.json()}
        assert items["custom-fast"]["source"] == "tenant"
        assert items["custom-fast"]["version"] == 2
        assert items["default"]["source"] == "builtin"

        # 详情：租户配置优先并携带执行计划
        detail = await client.get("/v1/pipelines/custom-fast", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["source"] == "tenant"
        assert detail.json()["execution_plan"]["pipeline_name"] == "custom-fast"

        # 非法 YAML → 400；name 不一致 → 400
        broken = await client.put(
            "/v1/pipelines/broken",
            json={"yaml_text": "schema_version: '1.0'\nname: broken\npipelines: {}\n"},
            headers=headers,
        )
        assert broken.status_code == 422
        mismatched = await client.put(
            "/v1/pipelines/other-name",
            json={"yaml_text": _custom_pipeline_yaml("wrong-name")},
            headers=headers,
        )
        assert mismatched.status_code == 422

        # viewer 403（写配置需 tenant admin）
        _v, viewer_tenant, _ = await create_user_with_tenant(container, username="pipe-viewer", role="viewer")
        viewer_headers = await _login(client, "pipe-viewer", viewer_tenant)
        denied = await client.put(
            "/v1/pipelines/custom-fast",
            json={"yaml_text": _custom_pipeline_yaml("custom-fast")},
            headers=viewer_headers,
        )
        assert denied.status_code == 403


async def test_submit_consumes_tenant_pipeline_and_snapshot_execution(container) -> None:
    """提交消费租户配置（快照冻结 YAML）；worker 仅凭快照执行（无需重传 pipeline）。"""
    username = "pipe-owner"
    user_id, tenant_id, _ = await create_user_with_tenant(container, username=username)
    scope = _scope(tenant_id, user_id)

    # 租户保存自定义 pipeline（内容=默认链 + 自定义名，证明消费的是租户版本）
    await container.configuration.save_pipeline(
        tenant_id,
        name="custom-fast",
        yaml_text=_custom_pipeline_yaml("custom-fast"),
        expected_version=None,
        actor_kind="user",
        actor_id=user_id,
    )

    dataset = await container.catalog.create_dataset(
        scope, name="ds-pipe", description=None, visibility="tenant"
    )
    body = b"pipeline snapshot\n"
    upload = await container.uploads.init_upload(
        scope, dataset_id=dataset.id, filename="note.txt", declared_total_bytes=len(body)
    )
    await container.uploads.put_part(scope, upload.id, 1, iter([body]))
    artifact_id = await container.uploads.complete_upload(scope, upload.id)
    result = await container.submit_parsing.submit(
        scope,
        artifact_id=artifact_id,
        dataset_id=dataset.id,
        pipeline_ref="custom-fast",
        document_id=None,
        expected_revision=None,
        filename="note.txt",
    )

    job = await container.jobs.get_job_by_id(result.job_id)
    assert job is not None
    plan = job.execution_plan
    assert plan["pipeline_name"] == "custom-fast"
    assert "yaml_text" in plan and "custom-fast" in plan["yaml_text"]

    # worker 不带 pipeline：执行器从快照重建并完成（快照与执行一致）
    await container.run_parsing_job._execute(result.job_id, "worker-test", None)
    job = await container.jobs.get_job(tenant_id, result.job_id)
    assert job.state == "DONE", job.error


async def test_default_pipeline_name_can_be_overridden_by_tenant(container) -> None:
    """租户保存与内置同名的配置后，提交（pipeline_ref=None）使用租户版本。"""
    username = "pipe-override"
    user_id, tenant_id, _ = await create_user_with_tenant(container, username=username)

    default_doc = yaml_lib.safe_load(pipeline_module.load_default_pipeline_yaml())
    default_name = default_doc["name"]
    builtin_digest = container.submit_parsing.compile_default_pipeline().digest

    await container.configuration.save_pipeline(
        tenant_id,
        name=default_name,
        yaml_text=_custom_pipeline_yaml(default_name, revision=42),
        expected_version=None,
        actor_kind="user",
        actor_id=user_id,
    )

    scope = _scope(tenant_id, user_id)
    dataset = await container.catalog.create_dataset(
        scope, name="ds-override", description=None, visibility="tenant"
    )
    body = b"override pipeline\n"
    upload = await container.uploads.init_upload(
        scope, dataset_id=dataset.id, filename="note.txt", declared_total_bytes=len(body)
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
        filename="note.txt",
    )
    job = await container.jobs.get_job_by_id(result.job_id)
    assert job is not None
    assert job.execution_plan["pipeline_name"] == default_name
    assert job.execution_plan["pipeline_revision"] == 42  # 租户版本生效
    version = await container.catalog.get_version(scope, result.document_id, result.version_id)
    assert version is not None
    # 有效配置摘要来自租户版本而非内置
    assert builtin_digest != job.execution_plan["config_digest"]
