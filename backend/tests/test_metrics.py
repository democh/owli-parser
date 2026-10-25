"""平台监控指标测试：任务积压/租约心跳/outbox 滞后 gauge 与 Token 拒绝计数。

（文档："平台监控提供 worker 心跳、最老任务、outbox lag、磁盘和 Token 拒绝统计"）
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from owli_parser.bootstrap.app import create_app
from owli_parser.core.context import ActorKind
from owli_parser.core.errors import NotAuthenticatedError
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.service import CatalogScope
from owli_parser.modules.jobs.models import ParsingJobRow
from tests.conftest import create_user_with_tenant


def _scope(tenant_id: uuid.UUID, user_id: uuid.UUID) -> CatalogScope:
    return CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )


async def _submit_job(container, username: str):
    user_id, tenant_id, _ = await create_user_with_tenant(container, username=username)
    scope = _scope(tenant_id, user_id)
    dataset = await container.catalog.create_dataset(
        scope, name=f"ds-metrics-{username}", description=None, visibility="tenant"
    )
    body = b"metrics gauge\n"
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
    return tenant_id, result.job_id


async def _metrics_text(container) -> str:
    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/metrics")
    assert response.status_code == 200, response.text
    return response.text


async def test_runtime_gauges_reflect_backlog_and_leases(container) -> None:
    tenant_id, job_id = await _submit_job(container, "metrics-a")

    # 任务待执行（PENDING）+ outbox 未派发（RecordingDispatcher 不投递）→ 积压可见
    text = await _metrics_text(container)
    assert "owli_jobs_pending_total 1" in text
    assert "owli_outbox_pending_total 1" in text
    assert "owli_jobs_oldest_pending_seconds 0.000" in text  # 固定时钟：创建即同刻
    assert "owli_active_leases_total 0" in text

    # worker 领取租约 → 心跳（活跃租约）可见
    fence = await container.jobs.acquire_lease(job_id, "worker-metrics")
    assert fence is not None
    text = await _metrics_text(container)
    assert "owli_active_leases_total 1" in text

    # outbox 滞后：把最老 pending 的 available_at 拨到过去 → lag 增长且被归一为非负
    async with container.uow.begin() as session:
        await session.execute(
            update(ParsingJobRow).where(ParsingJobRow.id == job_id).values(created_at=container.clock.now())
        )
    past = container.clock.now() - timedelta(seconds=45)
    from owli_parser.infrastructure.crosscut_models import OutboxEventRow

    async with container.uow.begin() as session:
        await session.execute(
            update(OutboxEventRow).where(OutboxEventRow.delivery_state == "pending").values(available_at=past)
        )
    text = await _metrics_text(container)
    assert "owli_outbox_oldest_pending_seconds 45.000" in text

    # 任务终态后积压归零
    async with container.uow.begin() as session:
        await session.execute(update(ParsingJobRow).where(ParsingJobRow.id == job_id).values(state="DONE"))
    text = await _metrics_text(container)
    assert "owli_jobs_pending_total 0" in text
    del tenant_id


async def test_stage_duration_histogram_rendering(container) -> None:
    """耗时直方图导出：固定桶累计计数、+Inf 与 sum（供 P50/P95 分位数）。"""
    from owli_parser.infrastructure.telemetry import METRICS

    METRICS.observe("owli_test_stage_duration_seconds", 0.2, {"stage": "unit"})
    METRICS.observe("owli_test_stage_duration_seconds", 5.0, {"stage": "unit"})
    text = await _metrics_text(container)
    assert 'owli_test_stage_duration_seconds_bucket{stage="unit",le="0.1"} 0' in text
    assert 'owli_test_stage_duration_seconds_bucket{stage="unit",le="0.25"} 1' in text
    assert 'owli_test_stage_duration_seconds_bucket{stage="unit",le="10"} 2' in text
    assert 'owli_test_stage_duration_seconds_bucket{stage="unit",le="+Inf"} 2' in text
    assert 'owli_test_stage_duration_seconds_count{stage="unit"} 2' in text
    assert 'owli_test_stage_duration_seconds_sum{stage="unit"} 5.200000' in text


async def test_stage_durations_recorded_across_pipeline(container) -> None:
    """上传/排队/解析/分块/索引/发布各段均有耗时观测（固定时钟下为 0）。"""
    _, job_id = await _submit_job(container, "metrics-stage")
    await container.run_parsing_job._execute(job_id, "worker-test", None)
    text = await _metrics_text(container)
    for stage in ("upload", "queue", "parsing", "normalizing", "chunking", "indexing", "publishing"):
        assert f'owli_stage_duration_seconds_count{{stage="{stage}"}}' in text


async def test_http_request_duration_histogram(container) -> None:
    """API 耗时直方图：首次拉取在响应后才被中间件观测，第二次拉取即可见。"""
    await _metrics_text(container)
    text = await _metrics_text(container)
    assert 'owli_http_request_duration_seconds_count{method="GET",status="200"}' in text
    assert 'owli_http_request_duration_seconds_bucket{method="GET",status="200",le="+Inf"}' in text


async def test_metrics_disabled_returns_404(container, monkeypatch) -> None:
    container.settings.observability.metrics_enabled = False
    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/metrics")
    assert response.status_code == 404
    assert "disabled" in response.text


async def test_token_rejection_counter(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="metrics-tok")
    application = await container.access.create_application(
        tenant_id,
        name="metrics-app",
        description=None,
        allowed_scopes=["datasets:read"],
        created_by_id=user_id,
    )
    issued = await container.access.issue_token(
        tenant_id,
        application.id,
        name="metrics-token",
        scopes=["datasets:read"],
        dataset_ids=[],
        expires_in_seconds=3600,
        created_by_id=user_id,
    )

    # 摘要不符（篡改 secret 尾字符，格式合法）→ invalid 拒绝计数
    tampered = issued.raw_token[:-1] + ("0" if issued.raw_token[-1] != "0" else "1")
    with pytest.raises(NotAuthenticatedError):
        await container.access.authenticate_token(tampered)
    text = await _metrics_text(container)
    assert 'owli_token_rejected_total{reason="invalid"}' in text

    # 撤销 token → 同样归入 invalid 粗粒度（不泄露具体拒绝原因）
    await container.access.revoke_token(tenant_id, application.id, issued.token_id, actor_id=user_id)
    with pytest.raises(NotAuthenticatedError):
        await container.access.authenticate_token(issued.raw_token)
    text = await _metrics_text(container)
    assert 'owli_token_rejected_total{reason="invalid"}' in text
