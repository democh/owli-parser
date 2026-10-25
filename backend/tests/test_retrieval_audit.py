"""检索审计测试：结构化日志（不落查询原文）与有界指标计数。"""

from __future__ import annotations

import logging

from owli_parser.adapters.retrieval.in_memory import InMemoryDenseIndex, InMemoryLexicalIndex
from owli_parser.bootstrap.settings import RetrievalSection
from owli_parser.infrastructure.telemetry import METRICS
from owli_parser.modules.retrieval.service import RetrievalService, _query_fingerprint
from owli_parser.ports.retrieval import IndexDocument


def _doc(doc_id: str, text: str) -> IndexDocument:
    return IndexDocument(
        id=doc_id,
        vector=None,
        text=text,
        tenant_id="t-audit",
        dataset_id="ds-audit",
        version_id="v1",
        chunk_id=doc_id,
        chunk_revision=1,
        index_generation=1,
        acl_ref="dataset:ds-audit",
    )


def _service() -> tuple[RetrievalService, InMemoryLexicalIndex]:
    lexical = InMemoryLexicalIndex()
    dense = InMemoryDenseIndex()
    service = RetrievalService(
        RetrievalSection(
            enabled=True,
            per_channel_limit=10,
            candidate_limit=20,
            top_k=5,
            rrf_k=60,
            channel_weights={"dense": 1.0, "lexical": 1.0},
            min_relevance_threshold=0.0,
        ),
        dense_index=dense,
        lexical_index=lexical,
    )
    return service, lexical


async def _allow_all(candidates) -> set[str]:
    return {hit.id for hit in candidates}


async def test_retrieval_query_log_fields(caplog) -> None:
    service, lexical = _service()
    lexical.upsert([_doc("c1", text="量子计算导论"), _doc("c2", text="量子纠缠实验")])
    caplog.set_level(logging.INFO, logger="owli.retrieval")

    result = await service.search(
        tenant_id="t-audit",
        query="量子",
        query_vector=None,
        dataset_ids=["ds-audit"],
        acl_refs=["dataset:ds-audit"],
        authoritative_filter=_allow_all,
    )
    assert result.channels_used == ["lexical"]

    records = [r for r in caplog.records if r.message == "retrieval_query"]
    assert len(records) == 1
    record = records[0]
    assert record.tenant_id == "t-audit"  # type: ignore[attr-defined]
    assert record.query_fingerprint == _query_fingerprint("量子")  # type: ignore[attr-defined]
    assert record.query_length == 2  # type: ignore[attr-defined]
    assert record.channels == ["lexical"]  # type: ignore[attr-defined]
    assert record.channel_hits == {"lexical": 2}  # type: ignore[attr-defined]
    assert record.fused_candidates == 2  # type: ignore[attr-defined]
    assert record.dropped_by_authority == 0  # type: ignore[attr-defined]
    assert record.returned == 2  # type: ignore[attr-defined]
    assert record.low_relevance is False  # type: ignore[attr-defined]
    assert isinstance(record.duration_ms, float)  # type: ignore[attr-defined]

    # 审计日志不落查询原文
    assert "量子" not in caplog.text


async def test_retrieval_log_reports_authority_drops(caplog) -> None:
    service, lexical = _service()
    lexical.upsert([_doc("c1", text="检索质量"), _doc("c2", text="检索评估")])
    caplog.set_level(logging.INFO, logger="owli.retrieval")

    async def only_c1(candidates) -> set[str]:
        return {hit.id for hit in candidates if hit.id == "c1"}

    await service.search(
        tenant_id="t-audit",
        query="检索",
        query_vector=None,
        dataset_ids=None,
        acl_refs=None,
        authoritative_filter=only_c1,
    )
    record = next(r for r in caplog.records if r.message == "retrieval_query")
    assert record.dropped_by_authority == 1  # type: ignore[attr-defined]
    assert record.returned == 1  # type: ignore[attr-defined]


async def test_retrieval_metrics_are_counted(caplog) -> None:
    service, lexical = _service()
    lexical.upsert([_doc("c1", text="指标计数")])
    caplog.set_level(logging.INFO, logger="owli.retrieval")

    await service.search(
        tenant_id="t-audit",
        query="指标",
        query_vector=None,
        dataset_ids=None,
        acl_refs=None,
        authoritative_filter=_allow_all,
    )

    rendered = METRICS.render()
    assert 'owli_retrieval_queries_total{low_relevance="false"}' in rendered
    assert 'owli_retrieval_channel_queries_total{channel="lexical"}' in rendered


def test_query_fingerprint_stable_and_not_reversible() -> None:
    fingerprint = _query_fingerprint("敏感查询内容")
    assert fingerprint == _query_fingerprint("敏感查询内容")
    assert len(fingerprint) == 12
    assert "敏感" not in fingerprint
    assert fingerprint != _query_fingerprint("敏感查询内容 ")
