"""M5-2 最小切片：管线 A/B 稳定分桶、快照冻结、任务归属与 API。

- 分桶：同一 (experiment, artifact) 恒定；50% 流量下分布大致均衡；
- 提交：traffic=100 → B、0 → A，变体 YAML 与 assignment 冻结进 execution_plan；
- 暂停：不参与实验（无 experiment 键）；
- API：PUT 幂等更新（revision 递增）/GET/404/非法变体 422/viewer 403。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from httpx import ASGITransport, AsyncClient

from owli_parser.bootstrap.app import create_app
from owli_parser.core.context import ActorKind
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.service import CatalogScope
from tests.conftest import create_user_with_tenant

_PASSWORD = "correct-horse-battery"


def _variant_b_yaml(default_yaml: str) -> str:
    variant = default_yaml.replace("gte: 0.5", "gte: 0.1")
    assert variant != default_yaml, "默认 YAML 中应包含 text_layer_ratio 阈值 0.5"
    return variant


def _scope(user_id: uuid.UUID, tenant_id: uuid.UUID) -> CatalogScope:
    return CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )


@asynccontextmanager
async def _client(
    container: Any, username: str, *, role: str = "admin"
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID, AsyncClient, dict[str, str]]]:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username=username, role=role)
    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/v1/auth/login", json={"username": username, "password": _PASSWORD})
        headers = {
            "X-OWLI-CSRF": login.json()["csrf_token"],
            "X-Tenant-ID": str(tenant_id),
        }
        yield user_id, tenant_id, client, headers


async def _submit_text(container: Any, scope: CatalogScope, tag: str):
    dataset = await container.catalog.create_dataset(
        scope, name=f"ds-{tag}-{uuid.uuid4().hex[:6]}", description=None, visibility="tenant"
    )
    data = f"experiment sample {tag}".encode()
    upload = await container.uploads.init_upload(
        scope, dataset_id=dataset.id, filename="sample.txt", declared_total_bytes=len(data)
    )
    await container.uploads.put_part(scope, upload.id, 1, iter([data]))
    artifact_id = await container.uploads.complete_upload(scope, upload.id)
    result = await container.submit_parsing.submit(
        scope,
        artifact_id=artifact_id,
        dataset_id=dataset.id,
        pipeline_ref=None,
        document_id=None,
        expected_revision=None,
        filename="sample.txt",
    )
    job = await container.jobs.get_job(scope.tenant_id, result.job_id)
    return result, job


async def test_stable_bucketing_and_distribution(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="exp-bucket")
    default = container.submit_parsing.compile_default_pipeline()
    await container.configuration.upsert_pipeline_experiment(
        tenant_id,
        pipeline_name="default",
        name="bucket-exp",
        variant_a_yaml=default.yaml_text,
        variant_b_yaml=_variant_b_yaml(default.yaml_text),
        traffic_percent=50,
        active=True,
        actor_kind="user",
        actor_id=user_id,
    )

    first = await container.configuration.select_pipeline_experiment(
        tenant_id, "default", bucket_key="artifact-1"
    )
    again = await container.configuration.select_pipeline_experiment(
        tenant_id, "default", bucket_key="artifact-1"
    )
    assert first is not None and again is not None
    assert first[1]["variant"] == again[1]["variant"]
    assert first[1]["bucket"] == again[1]["bucket"]

    variants = []
    for index in range(200):
        selected = await container.configuration.select_pipeline_experiment(
            tenant_id, "default", bucket_key=f"artifact-{index}"
        )
        assert selected is not None
        variants.append(selected[1]["variant"])
    share_b = variants.count("B") / len(variants)
    assert 0.3 <= share_b <= 0.7, f"B 组占比异常：{share_b:.2f}"


async def test_submit_freezes_variant_and_records_assignment(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="exp-submit")
    scope = _scope(user_id, tenant_id)
    default = container.submit_parsing.compile_default_pipeline()
    variant_b = _variant_b_yaml(default.yaml_text)

    await container.configuration.upsert_pipeline_experiment(
        tenant_id,
        pipeline_name="default",
        name="submit-exp",
        variant_a_yaml=default.yaml_text,
        variant_b_yaml=variant_b,
        traffic_percent=100,
        active=True,
        actor_kind="user",
        actor_id=user_id,
    )
    result, job = await _submit_text(container, scope, "b")
    assert job.execution_plan["experiment"]["variant"] == "B"
    assert job.execution_plan["experiment"]["traffic_percent"] == 100
    assert "gte: 0.1" in job.execution_plan["yaml_text"]

    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/v1/auth/login", json={"username": "exp-submit", "password": _PASSWORD})
        response = await client.get(
            f"/v1/parsing/jobs/{result.job_id}",
            headers={
                "X-OWLI-CSRF": login.json()["csrf_token"],
                "X-Tenant-ID": str(tenant_id),
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["experiment"]["variant"] == "B"

    # 流量切到 0 → 全部 A
    await container.configuration.upsert_pipeline_experiment(
        tenant_id,
        pipeline_name="default",
        name="submit-exp",
        variant_a_yaml=default.yaml_text,
        variant_b_yaml=variant_b,
        traffic_percent=0,
        active=True,
        actor_kind="user",
        actor_id=user_id,
    )
    _, job_a = await _submit_text(container, scope, "a")
    assert job_a.execution_plan["experiment"]["variant"] == "A"
    assert "gte: 0.5" in job_a.execution_plan["yaml_text"]


async def test_paused_experiment_does_not_assign(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="exp-paused")
    scope = _scope(user_id, tenant_id)
    default = container.submit_parsing.compile_default_pipeline()
    await container.configuration.upsert_pipeline_experiment(
        tenant_id,
        pipeline_name="default",
        name="paused-exp",
        variant_a_yaml=default.yaml_text,
        variant_b_yaml=_variant_b_yaml(default.yaml_text),
        traffic_percent=100,
        active=False,
        actor_kind="user",
        actor_id=user_id,
    )
    _, job = await _submit_text(container, scope, "paused")
    assert "experiment" not in job.execution_plan


async def test_experiment_report_groups_jobs_by_variant(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="exp-report")
    scope = _scope(user_id, tenant_id)
    default = container.submit_parsing.compile_default_pipeline()
    variant_b = _variant_b_yaml(default.yaml_text)

    async def upsert(traffic: int) -> None:
        await container.configuration.upsert_pipeline_experiment(
            tenant_id,
            pipeline_name="default",
            name="report-exp",
            variant_a_yaml=default.yaml_text,
            variant_b_yaml=variant_b,
            traffic_percent=traffic,
            active=True,
            actor_kind="user",
            actor_id=user_id,
        )

    await upsert(100)
    done_result, _ = await _submit_text(container, scope, "report-done")
    await container.run_parsing_job._execute(done_result.job_id, "worker-test", None)
    await _submit_text(container, scope, "report-pending")

    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/v1/auth/login", json={"username": "exp-report", "password": _PASSWORD})
        headers = {
            "X-OWLI-CSRF": login.json()["csrf_token"],
            "X-Tenant-ID": str(tenant_id),
        }
        report = await client.get("/v1/pipelines/default/experiment/report", headers=headers)
        assert report.status_code == 200, report.text
        body = report.json()
        assert body["total_jobs"] == 2
        group_b = body["variants"]["B"]
        assert group_b["jobs"] == 2
        assert group_b["done"] == 1
        assert group_b["in_progress"] == 1
        assert group_b["avg_coverage"] == 1.0
        assert group_b["avg_elapsed_seconds"] is not None
        assert body["variants"]["A"]["jobs"] == 0

        # 流量切 0 后新增 A 组任务；历史 B 组任务仍归属同一实验
        await upsert(0)
        await _submit_text(container, scope, "report-a")
        updated = await client.get("/v1/pipelines/default/experiment/report", headers=headers)
        assert updated.status_code == 200
        data = updated.json()
        assert data["total_jobs"] == 3
        assert data["variants"]["A"]["jobs"] == 1
        assert data["variants"]["B"]["jobs"] == 2

        missing = await client.get("/v1/pipelines/other/experiment/report", headers=headers)
        assert missing.status_code == 404


async def test_experiment_report_aggregates_quality_metrics(container) -> None:
    """质量聚合：回归 cases 按候选版本归因到变体；仅比例类指标求均值。"""
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="exp-quality")
    scope = _scope(user_id, tenant_id)
    default = container.submit_parsing.compile_default_pipeline()
    await container.configuration.upsert_pipeline_experiment(
        tenant_id,
        pipeline_name="default",
        name="quality-exp",
        variant_a_yaml=default.yaml_text,
        variant_b_yaml=_variant_b_yaml(default.yaml_text),
        traffic_percent=100,
        active=True,
        actor_kind="user",
        actor_id=user_id,
    )
    result, job = await _submit_text(container, scope, "quality")
    await container.run_parsing_job._execute(result.job_id, "worker-test", None)
    version_id = str(job.version_id)

    def case(cer: float, reading: float, hypothesis: str = version_id) -> dict[str, Any]:
        return {
            "hypothesis_version_id": hypothesis,
            "metrics": {"text_cer": cer, "reading_order": reading, "table_pairs": 2},
        }

    golden = await container.knowledge.upsert_golden_set(
        name=f"quality-{uuid.uuid4().hex[:8]}", cases=[], tenant_id=tenant_id
    )
    await container.knowledge.create_golden_run(
        golden_set_id=golden.id,
        adapter_ref="parsing-compare",
        config_digest=None,
        metrics={
            "passed": True,
            "violations": [],
            "aggregate": {},
            "cases": [
                case(0.02, 0.8),
                case(0.04, 1.0),
                # 不属于本实验变体产物的 case 不计入
                case(0.9, 0.1, hypothesis=str(uuid.uuid4())),
            ],
        },
        state="passed",
    )

    # 其他租户的黄金集即使引用了同一版本，也不得计入
    _, other_tenant, _ = await create_user_with_tenant(container, username="exp-quality-other")
    other_set = await container.knowledge.upsert_golden_set(
        name=f"other-{uuid.uuid4().hex[:8]}", cases=[], tenant_id=other_tenant
    )
    await container.knowledge.create_golden_run(
        golden_set_id=other_set.id,
        adapter_ref="parsing-compare",
        config_digest=None,
        metrics={"cases": [case(0.5, 0.5)]},
        state="passed",
    )
    # 全局 CLI 集（tenant_id=None）对全部租户可见
    global_set = await container.knowledge.upsert_golden_set(
        name=f"global-{uuid.uuid4().hex[:8]}", cases=[], tenant_id=None
    )
    await container.knowledge.create_golden_run(
        golden_set_id=global_set.id,
        adapter_ref="parsing-compare",
        config_digest=None,
        metrics={"cases": [case(0.06, 0.7)]},
        state="passed",
    )

    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/v1/auth/login", json={"username": "exp-quality", "password": _PASSWORD})
        headers = {
            "X-OWLI-CSRF": login.json()["csrf_token"],
            "X-Tenant-ID": str(tenant_id),
        }
        report = await client.get("/v1/pipelines/default/experiment/report", headers=headers)
        assert report.status_code == 200, report.text
        body = report.json()
        group_b = body["variants"]["B"]
        assert group_b["jobs"] == 1
        assert group_b["quality_cases"] == 3
        assert group_b["quality_metrics"]["text_cer"] == 0.04  # (0.02+0.04+0.06)/3
        assert group_b["quality_metrics"]["reading_order"] == 0.8333
        assert "table_pairs" not in group_b["quality_metrics"]
        assert body["variants"]["A"]["quality_cases"] == 0
        assert body["variants"]["A"]["quality_metrics"] == {}


async def test_experiment_api_crud_and_permissions(container) -> None:
    async with _client(container, "exp-api") as (_, _, client, headers):
        default = container.submit_parsing.compile_default_pipeline().yaml_text
        body = {
            "name": "api-exp",
            "variant_a_yaml": default,
            "variant_b_yaml": _variant_b_yaml(default),
            "traffic_percent": 25,
            "active": True,
        }
        first = await client.put("/v1/pipelines/default/experiment", json=body, headers=headers)
        assert first.status_code == 200, first.text
        assert first.json()["revision"] == 1
        assert first.json()["variant_a_digest"] != first.json()["variant_b_digest"]

        second = await client.put(
            "/v1/pipelines/default/experiment", json={**body, "traffic_percent": 40}, headers=headers
        )
        assert second.status_code == 200
        assert second.json()["revision"] == 2
        assert second.json()["traffic_percent"] == 40

        fetched = await client.get("/v1/pipelines/default/experiment", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["name"] == "api-exp"
        assert fetched.json()["variant_b_yaml"] == body["variant_b_yaml"]

        missing = await client.get("/v1/pipelines/other/experiment", headers=headers)
        assert missing.status_code == 404

        invalid = await client.put(
            "/v1/pipelines/default/experiment",
            json={
                **body,
                "variant_b_yaml": body["variant_b_yaml"].replace("name: default", "name: other", 1),
            },
            headers=headers,
        )
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "validation_failed"

    async with _client(container, "exp-viewer", role="viewer") as (_, _, viewer, headers):
        denied = await viewer.put(
            "/v1/pipelines/default/experiment",
            json={
                "name": "denied",
                "variant_a_yaml": "not-a-pipeline",
                "variant_b_yaml": "not-a-pipeline",
                "traffic_percent": 10,
            },
            headers=headers,
        )
        assert denied.status_code == 403
