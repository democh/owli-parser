"""质量回归执行装配：为 CLI 与 API 提供统一的 search_fn 工厂。

- 走真实检索管线（RetrievalService）并在返回前做 PG 权威校验（revision/enabled），
  与 /v1/retrieval/test 的语义一致；
- search_fn 接收 case（query/dataset_ids），返回权威过滤后的有序 chunk id 列表。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import select

from owli_parser.core.errors import NotFoundError, ValidationFailedError
from owli_parser.modules.catalog.models import DocumentRow, DocVersionRow
from owli_parser.modules.knowledge.models import ChunkRow
from owli_parser.modules.knowledge.service import GoldenSetInfo
from owli_parser.modules.parsing.compare import ParsingComparison, compare_ir_blocks
from owli_parser.modules.retrieval.domain import FusedHit


def make_retrieval_search_fn(
    container: Any, tenant_id: uuid.UUID
) -> Callable[[dict[str, Any]], Awaitable[list[str]]]:
    """构造黄金集评估的 search_fn（CLI 与 API 共用；确保评估口径一致）。"""

    async def search_fn(case: dict[str, Any]) -> list[str]:
        query = str(case["query"])
        dataset_ids = [str(x) for x in case.get("dataset_ids", [])]

        async def authority(candidates: Sequence[FusedHit]) -> set[str]:
            specs: dict[str, int] = {}
            for hit in candidates:
                chunk_id = hit.payload.get("chunk_id")
                revision = hit.payload.get("chunk_revision")
                if isinstance(chunk_id, str) and isinstance(revision, int):
                    specs[chunk_id] = revision
            if not specs:
                return set()
            async with container.uow.read_only() as session:
                rows = (
                    await session.execute(
                        select(ChunkRow.id, ChunkRow.current_revision, ChunkRow.enabled).where(
                            ChunkRow.tenant_id == tenant_id,
                            ChunkRow.id.in_([uuid.UUID(key) for key in specs]),
                        )
                    )
                ).all()
            valid = {str(row[0]) for row in rows if row[2] and row[1] == specs[str(row[0])]}
            return {hit.id for hit in candidates if str(hit.payload.get("chunk_id")) in valid}

        result = await container.retrieval.search(
            tenant_id=str(tenant_id),
            query=query,
            query_vector=None,
            dataset_ids=dataset_ids or None,
            acl_refs=[f"dataset:{d}" for d in dataset_ids] or None,
            authoritative_filter=authority,
        )
        return [str(item.payload.get("chunk_id")) for item in result.items]

    return search_fn


# ---------------------------------------------------------------------------
# 解析质量对比（版本级）：读取两个版本的 IR artifact → 指标对比 → 可选落 golden_runs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsingComparisonRun:
    comparison: ParsingComparison
    passed: bool
    violations: list[str] = field(default_factory=list)
    run_id: str | None = None


async def _load_ir_blocks(
    container: Any, tenant_id: uuid.UUID, version_id: uuid.UUID
) -> list[dict[str, Any]]:
    async with container.uow.read_only() as session:
        row = (
            await session.execute(
                select(DocVersionRow.ir_artifact_id, DocumentRow.tenant_id)
                .join(DocumentRow, DocumentRow.id == DocVersionRow.document_id)
                .where(DocVersionRow.id == version_id)
            )
        ).one_or_none()
    if row is None or row[1] != tenant_id:
        # 跨租户不泄露存在性
        raise NotFoundError("版本不存在")
    if row[0] is None:
        raise NotFoundError("该版本尚未生成 IR")
    location, profile = await container.artifacts.resolve_location(tenant_id, row[0])
    reader = container.artifacts.open_reader(location, profile)
    try:
        data: dict[str, Any] = json.loads(reader.read())
    finally:
        reader.close()
    return cast(list[dict[str, Any]], data.get("blocks") or [])


async def run_parsing_comparison(
    container: Any,
    *,
    tenant_id: uuid.UUID,
    reference_version_id: uuid.UUID,
    hypothesis_version_id: uuid.UUID,
    adapter_ref: str = "parsing-compare",
    golden_set_name: str = "parsing-compare",
    max_cer: float | None = None,
    min_reading_order: float | None = None,
    record: bool = True,
) -> ParsingComparisonRun:
    """对比两个版本的解析质量；阈值（max_cer/min_reading_order）未达即 violation。"""
    reference_blocks = await _load_ir_blocks(container, tenant_id, reference_version_id)
    hypothesis_blocks = await _load_ir_blocks(container, tenant_id, hypothesis_version_id)
    comparison = compare_ir_blocks(reference_blocks, hypothesis_blocks)

    violations: list[str] = []
    if max_cer is not None and comparison.text_cer > max_cer:
        violations.append(f"text_cer {comparison.text_cer:.4f} > 上限 {max_cer:.4f}")
    if min_reading_order is not None and comparison.reading_order < min_reading_order:
        violations.append(f"reading_order {comparison.reading_order:.4f} < 下限 {min_reading_order:.4f}")
    passed = not violations

    run_id: str | None = None
    if record:
        golden = await container.knowledge.upsert_golden_set(name=golden_set_name, cases=[], expectations={})
        run = await container.knowledge.create_golden_run(
            golden_set_id=golden.id,
            adapter_ref=adapter_ref,
            config_digest=None,
            metrics={
                "passed": passed,
                "violations": violations,
                "aggregate": comparison.as_metrics(),
                "reference_version_id": str(reference_version_id),
                "hypothesis_version_id": str(hypothesis_version_id),
            },
            state="passed" if passed else "failed",
        )
        run_id = run.run_id
    return ParsingComparisonRun(comparison=comparison, passed=passed, violations=violations, run_id=run_id)


# ---------------------------------------------------------------------------
# 批量解析回归：一集多 case（版本对）→ 宏平均聚合 + 阈值判定 → 单条 golden_run
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsingRegressionOutcome:
    case_results: list[dict[str, Any]]
    aggregate: dict[str, float]
    passed: bool
    violations: list[str] = field(default_factory=list)
    run_id: str | None = None


def _mean_present(values: list[float]) -> float:
    return round(sum(values) / len(values), 6)


async def run_parsing_regression(
    container: Any,
    *,
    tenant_id: uuid.UUID,
    golden_set: GoldenSetInfo,
    max_cer: float | None = None,
    min_reading_order: float | None = None,
    adapter_ref: str = "parsing-compare",
    record: bool = True,
) -> ParsingRegressionOutcome:
    """批量对比集：cases=[{reference_version_id, hypothesis_version_id, groups?}]。

    聚合为"仅存在值的宏平均"（缺失维度不按 0 计入——与"缺失不伪造"一致）；
    阈值对聚合值判定；逐 case 明细与聚合一并落 golden_runs。
    """
    if not golden_set.cases:
        raise ValidationFailedError("解析回归集不允许为空")

    case_results: list[dict[str, Any]] = []
    for index, case in enumerate(golden_set.cases):
        try:
            reference_id = uuid.UUID(str(case["reference_version_id"]))
            hypothesis_id = uuid.UUID(str(case["hypothesis_version_id"]))
        except (KeyError, ValueError) as exc:
            raise ValidationFailedError(f"case[{index}] 缺少合法的版本对字段") from exc
        reference_blocks = await _load_ir_blocks(container, tenant_id, reference_id)
        hypothesis_blocks = await _load_ir_blocks(container, tenant_id, hypothesis_id)
        comparison = compare_ir_blocks(reference_blocks, hypothesis_blocks)
        case_results.append(
            {
                "case": index,
                "reference_version_id": str(reference_id),
                "hypothesis_version_id": str(hypothesis_id),
                "groups": dict(case.get("groups") or {}),
                "metrics": comparison.as_metrics(),
            }
        )

    # 宏平均：仅对包含该键的 case 平均（缺失 case 不参与，不填 0）
    keys = sorted({key for result in case_results for key in result["metrics"]})
    aggregate = {
        key: _mean_present([result["metrics"][key] for result in case_results if key in result["metrics"]])
        for key in keys
    }

    violations: list[str] = []
    if max_cer is not None and aggregate.get("text_cer", 0.0) > max_cer:
        violations.append(f"text_cer 聚合 {aggregate.get('text_cer', 0.0):.4f} > 上限 {max_cer:.4f}")
    if min_reading_order is not None and aggregate.get("reading_order", 0.0) < min_reading_order:
        violations.append(
            f"reading_order 聚合 {aggregate.get('reading_order', 0.0):.4f} < 下限 {min_reading_order:.4f}"
        )
    passed = not violations

    run_id: str | None = None
    if record:
        run = await container.knowledge.create_golden_run(
            golden_set_id=golden_set.id,
            adapter_ref=adapter_ref,
            config_digest=None,
            metrics={
                "passed": passed,
                "violations": violations,
                "aggregate": aggregate,
                "cases": case_results,
            },
            state="passed" if passed else "failed",
        )
        run_id = run.run_id
    return ParsingRegressionOutcome(
        case_results=case_results,
        aggregate=aggregate,
        passed=passed,
        violations=violations,
        run_id=run_id,
    )
