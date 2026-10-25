"""版本级解析对比运行测试：真实两版本对比、阈值判定、落 golden_runs 与跨租户拒绝。"""

from __future__ import annotations

import uuid

import pytest

from owli_parser.bootstrap.quality_runner import (
    run_parsing_comparison,
    run_parsing_regression,
)
from owli_parser.core.context import ActorKind
from owli_parser.core.errors import NotFoundError, ValidationFailedError
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


async def _parse_text(container, scope, dataset_id, body: bytes, *, document_id=None):
    upload = await container.uploads.init_upload(
        scope, dataset_id=dataset_id, filename="doc.txt", declared_total_bytes=len(body)
    )
    await container.uploads.put_part(scope, upload.id, 1, iter([body]))
    artifact_id = await container.uploads.complete_upload(scope, upload.id)
    result = await container.submit_parsing.submit(
        scope,
        artifact_id=artifact_id,
        dataset_id=dataset_id,
        pipeline_ref=None,
        document_id=document_id,
        expected_revision=None,
        filename="doc.txt",
    )
    pipeline = container.submit_parsing.compile_default_pipeline()
    await container.run_parsing_job._execute(result.job_id, "worker-test", pipeline)
    return result


async def test_compare_two_versions_identical_and_record(container) -> None:
    """同内容两版本 → 完美基线；结果落 golden_runs（质量页可见）。"""
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="cmp-run")
    scope = _scope(tenant_id, user_id)
    dataset = await container.catalog.create_dataset(
        scope, name="ds-cmp-run", description=None, visibility="tenant"
    )
    body = "第一章 总则\n本章说明系统目标与交付边界。\n第二章 验收\n".encode()
    first = await _parse_text(container, scope, dataset.id, body)
    second = await _parse_text(container, scope, dataset.id, body, document_id=first.document_id)
    assert second.version_id != first.version_id  # 重新解析产生新版本

    outcome = await run_parsing_comparison(
        container,
        tenant_id=tenant_id,
        reference_version_id=first.version_id,
        hypothesis_version_id=second.version_id,
        max_cer=0.0,
        min_reading_order=1.0,
    )
    assert outcome.passed is True, outcome.violations
    assert outcome.comparison.text_cer == 0.0
    assert outcome.comparison.reading_order == 1.0
    assert outcome.run_id is not None

    runs = await container.knowledge.list_golden_runs(limit=5)
    recorded = next(run for run in runs if run.run_id == outcome.run_id)
    assert recorded.state == "passed"
    assert recorded.metrics["aggregate"]["text_cer"] == 0.0
    assert recorded.metrics["reference_version_id"] == str(first.version_id)


async def test_compare_different_content_fails_threshold(container) -> None:
    """不同内容两版本 → CER>0；max_cer=0 触发 violation 且落 failed。"""
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="cmp-diff")
    scope = _scope(tenant_id, user_id)
    dataset = await container.catalog.create_dataset(
        scope, name="ds-cmp-diff", description=None, visibility="tenant"
    )
    first = await _parse_text(container, scope, dataset.id, "基线版本内容。\n".encode())
    second = await _parse_text(
        container,
        scope,
        dataset.id,
        "完全不同的候选内容，与基线无关。\n".encode(),
        document_id=first.document_id,
    )

    outcome = await run_parsing_comparison(
        container,
        tenant_id=tenant_id,
        reference_version_id=first.version_id,
        hypothesis_version_id=second.version_id,
        max_cer=0.0,
    )
    assert outcome.passed is False
    assert outcome.comparison.text_cer > 0
    assert any("text_cer" in violation for violation in outcome.violations)

    runs = await container.knowledge.list_golden_runs(limit=5)
    recorded = next(run for run in runs if run.run_id == outcome.run_id)
    assert recorded.state == "failed"
    assert recorded.metrics["violations"]


async def test_compare_cross_tenant_and_missing_ir_rejected(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="cmp-sec")
    scope = _scope(tenant_id, user_id)
    dataset = await container.catalog.create_dataset(
        scope, name="ds-cmp-sec", description=None, visibility="tenant"
    )
    parsed = await _parse_text(container, scope, dataset.id, "内容。\n".encode())

    # 跨租户（错误 tenant_id）→ 404 不泄露存在性
    with pytest.raises(NotFoundError):
        await run_parsing_comparison(
            container,
            tenant_id=uuid.uuid4(),
            reference_version_id=parsed.version_id,
            hypothesis_version_id=parsed.version_id,
            record=False,
        )
    # 不存在的版本 → 404
    with pytest.raises(NotFoundError):
        await run_parsing_comparison(
            container,
            tenant_id=tenant_id,
            reference_version_id=uuid.uuid4(),
            hypothesis_version_id=parsed.version_id,
            record=False,
        )


async def test_batch_parsing_regression_aggregate_and_record(container) -> None:
    """批量回归：两 case（一对相同、一对不同）→ 宏平均、阈值判定与落库明细。"""
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="cmp-batch")
    scope = _scope(tenant_id, user_id)
    dataset = await container.catalog.create_dataset(
        scope, name="ds-cmp-batch", description=None, visibility="tenant"
    )
    # case 0：同内容重解析（完美）
    base = await _parse_text(container, scope, dataset.id, "基线内容文本。\n".encode())
    same = await _parse_text(
        container, scope, dataset.id, "基线内容文本。\n".encode(), document_id=base.document_id
    )
    # case 1：不同内容（CER>0）
    other_ref = await _parse_text(container, scope, dataset.id, "另一个文档基准。\n".encode())
    other_hyp = await _parse_text(
        container,
        scope,
        dataset.id,
        "彻底不同的候选文本内容。\n".encode(),
        document_id=other_ref.document_id,
    )

    golden = await container.knowledge.upsert_golden_set(
        name="batch-set",
        cases=[
            {
                "reference_version_id": str(base.version_id),
                "hypothesis_version_id": str(same.version_id),
                "groups": {"kind": "identical"},
            },
            {
                "reference_version_id": str(other_ref.version_id),
                "hypothesis_version_id": str(other_hyp.version_id),
                "groups": {"kind": "different"},
            },
        ],
        expectations={},
        tenant_id=tenant_id,
    )
    outcome = await run_parsing_regression(
        container,
        tenant_id=tenant_id,
        golden_set=golden,
        max_cer=2.0,  # 宽松：CER 可 >1（插入较多时），本用例仅验证聚合与落库
        min_reading_order=0.0,
    )
    assert len(outcome.case_results) == 2
    # case0 CER=0，case1 CER>0 → 聚合在两者之间（CER 无上界）
    assert outcome.aggregate["text_cer"] > 0.0
    assert outcome.aggregate["text_cer"] == pytest.approx(
        (0.0 + outcome.case_results[1]["metrics"]["text_cer"]) / 2, abs=1e-6
    )
    assert outcome.passed is True  # 阈值宽松
    assert outcome.run_id is not None

    runs = await container.knowledge.list_golden_runs(limit=5)
    recorded = next(run for run in runs if run.run_id == outcome.run_id)
    assert recorded.state == "passed"
    assert len(recorded.metrics["cases"]) == 2
    assert recorded.metrics["cases"][0]["groups"]["kind"] == "identical"


async def test_batch_parsing_regression_threshold_and_empty_guard(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="cmp-batch2")
    scope = _scope(tenant_id, user_id)
    dataset = await container.catalog.create_dataset(
        scope, name="ds-cmp-batch2", description=None, visibility="tenant"
    )
    ref = await _parse_text(container, scope, dataset.id, "甲文档内容段落。\n".encode())
    hyp = await _parse_text(
        container, scope, dataset.id, "乙文档完全不同的内容。\n".encode(), document_id=ref.document_id
    )
    golden = await container.knowledge.upsert_golden_set(
        name="batch-set2",
        cases=[{"reference_version_id": str(ref.version_id), "hypothesis_version_id": str(hyp.version_id)}],
        expectations={},
        tenant_id=tenant_id,
    )
    # 阈值严格 → violation + failed
    outcome = await run_parsing_regression(container, tenant_id=tenant_id, golden_set=golden, max_cer=0.0)
    assert outcome.passed is False
    runs = await container.knowledge.list_golden_runs(limit=5)
    recorded = next(run for run in runs if run.run_id == outcome.run_id)
    assert recorded.state == "failed"

    # 空集 → 明确拒绝（不假装通过）
    empty = await container.knowledge.upsert_golden_set(name="batch-empty", cases=[], expectations={})
    with pytest.raises(ValidationFailedError):
        await run_parsing_regression(container, tenant_id=tenant_id, golden_set=empty, record=False)

    # case 字段缺失 → 明确拒绝并指示下标
    malformed = await container.knowledge.upsert_golden_set(
        name="batch-malformed", cases=[{"hypothesis_version_id": str(hyp.version_id)}], expectations={}
    )
    with pytest.raises(ValidationFailedError, match="case\\[0\\]"):
        await run_parsing_regression(container, tenant_id=tenant_id, golden_set=malformed, record=False)
