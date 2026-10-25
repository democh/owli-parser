"""复核登记测试：目标收集纯函数、幂等重建、工作流接线（低置信/缺失单位 → 队列）。"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from owli_parser.core.context import ActorKind
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.service import CatalogScope
from owli_parser.modules.knowledge.models import ReviewItemRow
from owli_parser.modules.knowledge.review import ReviewTargets, collect_review_targets
from tests.conftest import create_user_with_tenant


class _ConfidenceStub:
    def __init__(self, raw_score: float) -> None:
        self.raw_score = raw_score


class _BlockStub:
    def __init__(self, block_id: str, raw_score: float | None) -> None:
        self.id = block_id
        self.confidence = _ConfidenceStub(raw_score) if raw_score is not None else None


def test_collect_review_targets_filters_and_dedups() -> None:
    blocks = [
        _BlockStub("b-low", 0.4),
        _BlockStub("b-edge", 0.5),  # == 阈值：严格小于才登记
        _BlockStub("b-ok", 0.9),
        _BlockStub("b-none", None),  # 未评估不误报
    ]
    targets = collect_review_targets(
        blocks=blocks, missing_units=["p3", "p1", "p3"], low_confidence_threshold=0.5
    )
    assert targets.low_confidence_block_ids == ["b-low"]
    assert targets.missing_units == ["p1", "p3"]  # 去重 + 排序

    empty = collect_review_targets(blocks=[], missing_units=[], low_confidence_threshold=0.5)
    assert empty == ReviewTargets(low_confidence_block_ids=[], missing_units=[])


async def test_enqueue_review_items_is_idempotent(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="rev-idem")
    version_id = uuid.uuid4()
    job_id = uuid.uuid4()

    async with container.uow.begin() as session:
        await container.knowledge.enqueue_review_items(
            tenant_id=tenant_id,
            version_id=version_id,
            job_id=job_id,
            block_ids=["p1", "p2"],
            reason="missing_unit",
            session=session,
        )
    async with container.uow.begin() as session:
        await container.knowledge.enqueue_review_items(
            tenant_id=tenant_id,
            version_id=version_id,
            job_id=job_id,
            block_ids=["p9"],
            reason="missing_unit",
            session=session,
        )
    # 同 reason 幂等重建：只剩最新一批
    items, _cursor, _has_more = await container.knowledge.list_review_items(
        tenant_id, status="open", cursor=None, limit=50
    )
    assert [item.block_id for item in items] == ["p9"]

    # 不同 reason 互不影响（各自保留）
    async with container.uow.begin() as session:
        await container.knowledge.enqueue_review_items(
            tenant_id=tenant_id,
            version_id=version_id,
            job_id=job_id,
            block_ids=["b-0000"],
            reason="low_confidence",
            session=session,
        )
    items, _cursor, _has_more = await container.knowledge.list_review_items(
        tenant_id, status="open", cursor=None, limit=50
    )
    assert {(item.reason, item.block_id) for item in items} == {
        ("missing_unit", "p9"),
        ("low_confidence", "b-0000"),
    }

    # 过滤：reason 与 version_id（工作台徽标 → 复核页动线）
    only_low, _, _ = await container.knowledge.list_review_items(
        tenant_id, status="open", cursor=None, limit=50, reason="low_confidence"
    )
    assert [item.block_id for item in only_low] == ["b-0000"]
    on_version, _, _ = await container.knowledge.list_review_items(
        tenant_id, status="open", cursor=None, limit=50, version_id=version_id
    )
    assert len(on_version) == 2
    other_version, _, _ = await container.knowledge.list_review_items(
        tenant_id, status="open", cursor=None, limit=50, version_id=uuid.uuid4()
    )
    assert other_version == []
    del user_id


async def test_parsing_workflow_enqueues_review_items(container, monkeypatch) -> None:
    """工作流接线：collect 产出非空时，按 reason 分别登记（真实解析链路）。"""
    from owli_parser.workflows import run_parsing_job as module

    monkeypatch.setattr(
        module,
        "collect_review_targets",
        lambda **kwargs: ReviewTargets(low_confidence_block_ids=["b-0000"], missing_units=["p9"]),
    )
    calls: list[dict] = []
    original = container.knowledge.enqueue_review_items

    async def spy(**kwargs):
        calls.append({"reason": kwargs["reason"], "block_ids": list(kwargs["block_ids"])})
        return await original(**kwargs)

    monkeypatch.setattr(container.knowledge, "enqueue_review_items", spy)

    user_id, tenant_id, _ = await create_user_with_tenant(container, username="rev-flow")
    scope = CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )
    dataset = await container.catalog.create_dataset(
        scope, name="ds-review", description=None, visibility="tenant"
    )
    body = b"hello review world\n"
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
    pipeline = container.submit_parsing.compile_default_pipeline()
    await container.run_parsing_job._execute(result.job_id, "worker-test", pipeline)

    job = await container.jobs.get_job(tenant_id, result.job_id)
    assert job.state == "DONE", job.error
    assert {call["reason"] for call in calls} == {"missing_unit", "low_confidence"}
    by_reason = {call["reason"]: call["block_ids"] for call in calls}
    assert by_reason["missing_unit"] == ["p9"]
    assert by_reason["low_confidence"] == ["b-0000"]

    # 队列可见（前端复核页的数据来源），document_id 供跳转工作台
    items, _cursor, _has_more = await container.knowledge.list_review_items(
        tenant_id, status="open", cursor=None, limit=50
    )
    assert {(item.reason, item.block_id) for item in items} == {
        ("missing_unit", "p9"),
        ("low_confidence", "b-0000"),
    }
    assert all(item.document_id == result.document_id for item in items)
    async with container.uow.read_only() as session:
        rows = (
            (await session.execute(select(ReviewItemRow).where(ReviewItemRow.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert all(row.version_id == result.version_id for row in rows)

    # 版本质量摘要：待复核计数（service 聚合 + versions API 响应）
    counts = await container.knowledge.count_open_reviews_by_version(tenant_id, [result.version_id])
    assert counts == {result.version_id: 2}
    assert await container.knowledge.count_open_reviews_by_version(tenant_id, []) == {}

    from httpx import ASGITransport, AsyncClient

    from owli_parser.bootstrap.app import create_app

    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post(
            "/v1/auth/login",
            json={"username": "rev-flow", "password": "correct-horse-battery"},
        )
        assert login.status_code == 200, login.text
        headers = {
            "X-OWLI-CSRF": login.json()["csrf_token"],
            "X-Tenant-ID": str(tenant_id),
        }
        versions_resp = await client.get(f"/v1/documents/{result.document_id}/versions", headers=headers)
        assert versions_resp.status_code == 200, versions_resp.text
        version_payload = next(v for v in versions_resp.json() if v["id"] == str(result.version_id))
        assert version_payload["pending_review_count"] == 2
