"""恢复矩阵测试（文档 §5"恢复"类）：租约过期回收、旧 fence 拒绝、终态不可领取。

覆盖"每阶段 worker 退出、重复消息、租约过期、旧 worker 迟到"的受控语义：
- 过期回收 → RETRY_WAIT + lease_expired 错误 + 事件 + on_expired 回调；
- 回收后新 worker 领取 fence 递增，**旧 fence 推进被拒**（不修改终态）；
- 终态任务的重复消息不可领取（重复消费幂等）。
"""

from __future__ import annotations

import uuid

from owli_parser.contracts.events import JobState
from owli_parser.core.context import ActorKind
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.service import CatalogScope
from tests.conftest import create_user_with_tenant


def _scope(tenant_id: uuid.UUID, user_id: uuid.UUID) -> CatalogScope:
    return CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )


async def _submitted_job(container, username: str):
    user_id, tenant_id, _ = await create_user_with_tenant(container, username=username)
    scope = _scope(tenant_id, user_id)
    dataset = await container.catalog.create_dataset(
        scope, name=f"ds-rec-{username}", description=None, visibility="tenant"
    )
    body = b"recovery matrix\n"
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


async def test_expired_lease_swept_then_stale_fence_rejected(container) -> None:
    tenant_id, job_id = await _submitted_job(container, "rec-a")

    # worker A 领取 → ROUTING + fence=1
    fence_a = await container.jobs.acquire_lease(job_id, "worker-a")
    assert fence_a == 1
    job = await container.jobs.get_job(tenant_id, job_id)
    assert job.state == JobState.ROUTING.value

    # 未过期前 sweep 不动它
    assert await container.jobs.sweep_expired_leases(batch=10) == 0

    # 时钟推进超过租约时长（worker A 崩溃）→ sweep 回收为 RETRY_WAIT
    container.clock.advance(container.settings.jobs.lease_seconds + 1)
    expired_ids: list[uuid.UUID] = []
    swept = await container.jobs.sweep_expired_leases(
        batch=10, on_expired=lambda job: expired_ids.append(job)
    )
    assert swept == 1
    assert expired_ids == [job_id]
    job = await container.jobs.get_job(tenant_id, job_id)
    assert job.state == JobState.RETRY_WAIT.value
    assert job.error is not None and job.error.get("code") == "lease_expired"
    events, _latest = await container.jobs.list_events(tenant_id, job_id, after_seq=0)
    assert any(event.event_type == "job.lease_expired" for event in events)

    # worker B 重新领取 → fence=2
    fence_b = await container.jobs.acquire_lease(job_id, "worker-b")
    assert fence_b == 2

    # 旧 worker A 迟到：旧 fence 推进被拒（不得修改状态）
    stale = await container.jobs.transition(
        job_id, fence_a, JobState.PARSING.value, stage=JobState.PARSING.value
    )
    assert stale is False
    job = await container.jobs.get_job(tenant_id, job_id)
    assert job.state == JobState.ROUTING.value  # 未被旧 worker 修改
    assert job.fence == 2

    # 新 fence 可正常推进（从已提交状态续跑）
    fresh = await container.jobs.transition(
        job_id, fence_b, JobState.PARSING.value, stage=JobState.PARSING.value
    )
    assert fresh is True
    job = await container.jobs.get_job(tenant_id, job_id)
    assert job.state == JobState.PARSING.value


async def test_terminal_job_not_acquirable(container) -> None:
    """重复消息/重复消费：终态任务不可再领取（worker 直接退出）。"""
    tenant_id, job_id = await _submitted_job(container, "rec-b")
    fence = await container.jobs.acquire_lease(job_id, "worker-a")
    assert fence == 1

    # 直接置为终态（模拟任务完成）→ 再领取返回 None
    await container.jobs.transition(job_id, fence, JobState.PARSING.value, stage=JobState.PARSING.value)
    from sqlalchemy import update

    from owli_parser.modules.jobs.models import ParsingJobRow

    async with container.uow.begin() as session:
        await session.execute(
            update(ParsingJobRow)
            .where(ParsingJobRow.id == job_id)
            .values(state=JobState.DONE.value, lease_owner=None, lease_until=None)
        )
    assert await container.jobs.acquire_lease(job_id, "worker-b") is None
    del tenant_id
