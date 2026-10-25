"""检索地基测试：RRF 数学、内存索引 contract、混合管线与权威校验、API 接线。

覆盖文档 §9.1 的不变量：
- RRF = Σ w_i/(rrf_k+rank_i)，rank 从 1 起，未命中通道贡献 0；
- raw_score 量纲不混合，仅通道内可比；
- 前置过滤（tenant/dataset/ACL）在索引检索阶段生效；
- 返回前必须经 PG 权威校验（revision/enabled 过期候选剔除）；
- 低相关与空召回显式告警，不伪造相关性。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from owli_parser.adapters.retrieval.in_memory import (
    InMemoryDenseIndex,
    InMemoryLexicalIndex,
    tokenize,
)
from owli_parser.bootstrap.app import create_app
from owli_parser.bootstrap.settings import RetrievalSection
from owli_parser.core.context import ActorKind
from owli_parser.core.errors import CapabilityNotReadyError
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.service import CatalogScope
from owli_parser.modules.retrieval.domain import RankedHit, is_low_relevance, rrf_fuse
from owli_parser.modules.retrieval.service import RetrievalService
from owli_parser.ports.retrieval import (
    DimensionMismatchError,
    IndexDocument,
    IndexFilter,
)
from tests.conftest import create_user_with_tenant

RRF_K = 60


def _hit(doc_id: str, score: float) -> RankedHit:
    return RankedHit(id=doc_id, raw_score=score)


# ---------------------------------------------------------------------------
# RRF 融合（纯函数）
# ---------------------------------------------------------------------------


def test_rrf_fuse_math_and_contributions() -> None:
    dense = [_hit("A", 0.9), _hit("B", 0.8), _hit("C", 0.7)]
    lexical = [_hit("B", 12.0), _hit("D", 11.0)]
    fused = rrf_fuse(
        {"dense": dense, "lexical": lexical},
        rrf_k=RRF_K,
        weights={"dense": 1.0, "lexical": 1.0},
        candidate_limit=10,
    )
    assert [h.id for h in fused] == ["B", "A", "D", "C"]
    by_id = {h.id: h for h in fused}
    # B: dense rank2 + lexical rank1
    assert by_id["B"].rrf_score == pytest.approx(1 / 62 + 1 / 61)
    assert by_id["B"].channel_ranks == {"dense": 2, "lexical": 1}
    assert by_id["B"].channel_raw_scores == {"dense": 0.8, "lexical": 12.0}
    # A 仅 dense rank1，lexical 未命中贡献为 0（无 lexical 条目）
    assert by_id["A"].rrf_score == pytest.approx(1 / 61)
    assert by_id["A"].channel_contributions == {"dense": pytest.approx(1 / 61)}
    # D 仅 lexical rank2
    assert by_id["D"].rrf_score == pytest.approx(1 / 62)


def test_rrf_weights_and_disabled_channel() -> None:
    dense = [_hit("A", 1.0)]
    lexical = [_hit("B", 1.0)]
    fused = rrf_fuse(
        {"dense": dense, "lexical": lexical},
        rrf_k=RRF_K,
        weights={"dense": 2.0, "lexical": 0.0},
        candidate_limit=10,
    )
    # lexical 权重 0 → 不参与融合，B 不得上榜
    assert [h.id for h in fused] == ["A"]
    assert fused[0].rrf_score == pytest.approx(2 / 61)


def test_rrf_tie_break_and_candidate_limit() -> None:
    fused = rrf_fuse(
        {"dense": [_hit("b", 1.0), _hit("a", 0.5)]},
        rrf_k=1,
        weights={"dense": 1.0},
        candidate_limit=10,
    )
    # rank 相同分数下按 id 升序（确定性输出）
    assert [h.id for h in fused] == ["b", "a"]
    limited = rrf_fuse(
        {"dense": [_hit("x", 3.0), _hit("y", 2.0), _hit("z", 1.0)]},
        rrf_k=RRF_K,
        weights={"dense": 1.0},
        candidate_limit=2,
    )
    assert [h.id for h in limited] == ["x", "y"]


def test_rrf_invalid_parameters() -> None:
    with pytest.raises(ValueError):
        rrf_fuse({}, rrf_k=0, weights={}, candidate_limit=10)
    with pytest.raises(ValueError):
        rrf_fuse({}, rrf_k=60, weights={}, candidate_limit=0)


def test_low_relevance_semantics() -> None:
    assert is_low_relevance({}, threshold=0.0) is True
    assert is_low_relevance({"dense": []}, threshold=0.0) is True
    assert is_low_relevance({"dense": [_hit("A", 0.2)]}, threshold=0.5) is True
    # 任一通道最高分达标即不算低相关
    assert is_low_relevance({"dense": [_hit("A", 0.2)], "lexical": [_hit("B", 3.0)]}, threshold=0.5) is False


# ---------------------------------------------------------------------------
# 内存索引 contract（M3 真实 adapter 将以同一套语义验收）
# ---------------------------------------------------------------------------


def _doc(
    doc_id: str,
    *,
    vector: list[float] | None = None,
    text: str = "",
    tenant: str = "t1",
    dataset: str = "ds1",
    generation: int = 1,
    acl: str = "dataset:ds1",
) -> IndexDocument:
    return IndexDocument(
        id=doc_id,
        vector=vector,
        text=text,
        tenant_id=tenant,
        dataset_id=dataset,
        version_id="v1",
        chunk_id=doc_id,
        chunk_revision=1,
        index_generation=generation,
        acl_ref=acl,
    )


def test_dense_index_contract() -> None:
    index = InMemoryDenseIndex()
    index.upsert(
        [
            _doc("c1", vector=[1.0, 0.0]),
            _doc("c2", vector=[0.9, 0.1], dataset="ds2", acl="dataset:ds2"),
            _doc("c3", vector=[0.0, 1.0]),
        ]
    )
    filter_ = IndexFilter(tenant_id="t1")
    hits = index.search([1.0, 0.0], limit=3, filter_=filter_)
    assert [h.id for h in hits] == ["c1", "c2", "c3"]
    assert hits[0].raw_score == pytest.approx(1.0)
    assert hits[0].payload["chunk_id"] == "c1"  # 权威校验字段随 payload 返回

    # 前置过滤：dataset / acl / tenant 隔离
    only_ds2 = index.search(
        [1.0, 0.0], limit=3, filter_=IndexFilter(tenant_id="t1", dataset_ids=frozenset({"ds2"}))
    )
    assert [h.id for h in only_ds2] == ["c2"]
    no_hits = index.search([1.0, 0.0], limit=3, filter_=IndexFilter(tenant_id="t-other"))
    assert no_hits == []

    # 维度混写被拒绝；查询维度不一致同样拒绝
    with pytest.raises(DimensionMismatchError):
        index.upsert([_doc("c4", vector=[1.0, 0.0, 0.0])])
    with pytest.raises(DimensionMismatchError):
        index.search([1.0], limit=1, filter_=filter_)

    # 幂等覆盖 + 旧代清理
    index.upsert([_doc("c3", vector=[1.0, 0.0])])
    hits = index.search([1.0, 0.0], limit=1, filter_=filter_)
    assert hits[0].raw_score == pytest.approx(1.0)
    index.upsert([_doc("c5", vector=[1.0, 0.0], generation=2)])
    assert index.delete_by_generation(tenant_id="t1", version_id="v1", before_generation=2) == 3
    remaining = index.search([1.0, 0.0], limit=10, filter_=filter_)
    assert [h.id for h in remaining] == ["c5"]


def test_lexical_index_contract() -> None:
    index = InMemoryLexicalIndex()
    index.upsert(
        [
            _doc("d1", text="检索增强生成系统设计"),
            _doc("d2", text="数据库索引优化", dataset="ds2", acl="dataset:ds2"),
            _doc("d3", text="retrieval augmented generation"),
        ]
    )
    filter_ = IndexFilter(tenant_id="t1")
    hits = index.search("检索 生成", limit=5, filter_=filter_)
    # 逐字 unigram 基线：单字命中也会上榜，但多词全命中者排名更前
    assert hits[0].id == "d1"
    assert hits[0].raw_score > 0
    if len(hits) > 1:
        assert hits[0].raw_score > hits[1].raw_score

    hits = index.search("retrieval", limit=5, filter_=filter_)
    assert [h.id for h in hits] == ["d3"]

    # 过滤与空查询
    assert index.search("检索", limit=5, filter_=IndexFilter(tenant_id="t-other")) == []
    assert index.search("   ", limit=5, filter_=filter_) == []

    # 覆盖写入：旧文本不再命中，df 正确回退
    index.upsert([_doc("d3", text="完全不同的内容")])
    assert index.search("retrieval", limit=5, filter_=filter_) == []
    assert [h.id for h in index.search("不同", limit=5, filter_=filter_)] == ["d3"]

    # tokenize 确定性（中文字 + ASCII 词小写）
    assert tokenize("Hello 世界 GB") == ["hello", "世", "界", "gb"]


# ---------------------------------------------------------------------------
# RetrievalService 混合管线
# ---------------------------------------------------------------------------


def _enabled_settings(**overrides) -> RetrievalSection:
    base = RetrievalSection(
        enabled=True,
        per_channel_limit=20,
        candidate_limit=30,
        top_k=10,
        rrf_k=RRF_K,
        channel_weights={"dense": 1.0, "lexical": 1.0},
        min_relevance_threshold=0.0,
    )
    return base.model_copy(update=overrides) if overrides else base


async def _allow_all(candidates) -> set[str]:
    return {hit.id for hit in candidates}


async def test_service_pipeline_end_to_end() -> None:
    dense = InMemoryDenseIndex()
    lexical = InMemoryLexicalIndex()
    dense.upsert(
        [
            _doc("c1", vector=[1.0, 0.0]),
            _doc("c2", vector=[0.7, 0.7]),
            _doc("c3", vector=[0.0, 1.0]),
        ]
    )
    lexical.upsert(
        [
            _doc("c1", text="检索增强生成"),
            _doc("c2", text="检索质量评估"),
            _doc("c3", text="数据库"),
        ]
    )
    service = RetrievalService(_enabled_settings(), dense_index=dense, lexical_index=lexical)

    result = await service.search(
        tenant_id="t1",
        query="检索",
        query_vector=[1.0, 0.0],
        dataset_ids=["ds1"],
        acl_refs=["dataset:ds1"],
        authoritative_filter=_allow_all,
    )
    assert result.channels_used == ["dense", "lexical"]
    assert next(item.id for item in result.items) == "c1"
    assert result.low_relevance is False
    assert result.applied_filters["tenant_id"] == "t1"
    c1 = next(item for item in result.items if item.id == "c1")
    assert c1.channel_ranks == {"dense": 1, "lexical": 1}
    assert c1.rerank_score is None


async def test_service_authoritative_filter_drops_stale() -> None:
    lexical = InMemoryLexicalIndex()
    lexical.upsert([_doc("c1", text="检索增强"), _doc("c2", text="检索评估")])
    service = RetrievalService(_enabled_settings(), dense_index=None, lexical_index=lexical)

    async def only_c1(candidates) -> set[str]:
        return {hit.id for hit in candidates if hit.id == "c1"}

    result = await service.search(
        tenant_id="t1",
        query="检索",
        query_vector=None,
        dataset_ids=None,
        acl_refs=None,
        authoritative_filter=only_c1,
    )
    assert [item.id for item in result.items] == ["c1"]
    assert any("未通过权威校验" in w for w in result.warnings)
    assert result.channels_used == ["lexical"]


async def test_service_low_relevance_and_top_k() -> None:
    lexical = InMemoryLexicalIndex()
    lexical.upsert([_doc(f"c{i}", text="检索") for i in range(5)])
    service = RetrievalService(
        _enabled_settings(min_relevance_threshold=999.0, top_k=2),
        dense_index=None,
        lexical_index=lexical,
    )
    result = await service.search(
        tenant_id="t1",
        query="检索",
        query_vector=None,
        dataset_ids=None,
        acl_refs=None,
        authoritative_filter=_allow_all,
    )
    assert len(result.items) == 2
    assert result.low_relevance is True
    assert any("低于相关性阈值" in w for w in result.warnings)


async def test_service_disabled_raises() -> None:
    service = RetrievalService(RetrievalSection(enabled=False), dense_index=None, lexical_index=None)
    with pytest.raises(CapabilityNotReadyError):
        await service.search(
            tenant_id="t1",
            query="q",
            query_vector=None,
            dataset_ids=None,
            acl_refs=None,
            authoritative_filter=_allow_all,
        )


# ---------------------------------------------------------------------------
# API 接线：enabled=true 时真实走管线，并经 PG 权威校验
# ---------------------------------------------------------------------------


def _scope(tenant_id: uuid.UUID, user_id: uuid.UUID) -> CatalogScope:
    return CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )


async def _login(client: AsyncClient, username: str, tenant_id: uuid.UUID) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/login", json={"username": username, "password": "correct-horse-battery"}
    )
    assert response.status_code == 200, response.text
    return {
        "X-OWLI-CSRF": response.json()["csrf_token"],
        "X-Tenant-ID": str(tenant_id),
    }


async def _setup_indexed_document(container, username: str):
    """上传解析 → 发布 → 将首个可用 chunk 写入索引；返回 (tenant, dataset, target)。"""
    user_id, tenant_id, _ = await create_user_with_tenant(container, username=username)
    scope = _scope(tenant_id, user_id)
    dataset = await container.catalog.create_dataset(
        scope, name=f"ds-{username}", description=None, visibility="tenant"
    )
    upload = await container.uploads.init_upload(
        scope, dataset_id=dataset.id, filename="note.txt", declared_total_bytes=22
    )
    await container.uploads.put_part(scope, upload.id, 1, iter([b"hello retrieval world\n"]))
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

    chunks, _, _ = await container.knowledge.list_chunks(
        tenant_id, document_id=result.document_id, version_id=None, cursor=None, limit=50
    )
    enabled_chunks = [c for c in chunks if c.enabled]
    assert enabled_chunks, "解析后应有可用 chunk"
    target = enabled_chunks[0]
    container.lexical_index.upsert(
        [
            IndexDocument(
                id=f"{tenant_id}:{target.version_id}:{target.id}:{target.current_revision}:1",
                vector=None,
                text=target.text,
                tenant_id=str(tenant_id),
                dataset_id=str(dataset.id),
                version_id=str(target.version_id),
                chunk_id=str(target.id),
                chunk_revision=target.current_revision,
                index_generation=1,
                acl_ref=f"dataset:{dataset.id}",
            )
        ]
    )
    container.settings.retrieval.enabled = True  # 同一对象被 service 持有
    return tenant_id, dataset.id, target


async def test_retrieval_api_with_pg_authoritative_check(container) -> None:
    """完整链路：上传解析产出真实 chunk → 写入索引 → API 检索经 PG 复核返回。"""
    username = "retrieval-admin"
    tenant_id, dataset_id, target = await _setup_indexed_document(container, username)

    app = create_app(container)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, username, tenant_id)
        response = await client.post(
            "/v1/retrieval/test",
            json={"query": "retrieval", "dataset_ids": [str(dataset_id)], "top_k": 5},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["channels_used"] == ["lexical"]
        assert payload["low_relevance"] is False
        assert payload["applied_filters"]["dataset_ids"] == [str(dataset_id)]
        assert len(payload["items"]) == 1
        item = payload["items"][0]
        assert item["payload"]["chunk_id"] == str(target.id)
        assert item["channel_ranks"] == {"lexical": 1}
        assert item["channel_contributions"]["lexical"] == pytest.approx(1 / 61)

        # revision 过期（模拟并发编辑）→ 权威校验剔除候选
        async with container.uow.begin() as session:
            from owli_parser.modules.knowledge.models import ChunkRow

            row = (await session.execute(select(ChunkRow).where(ChunkRow.id == target.id))).scalar_one()
            row.current_revision += 1
        response = await client.post(
            "/v1/retrieval/test",
            json={"query": "retrieval", "dataset_ids": [str(dataset_id)], "top_k": 5},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["items"] == []
        assert any("未通过权威校验" in w for w in payload["warnings"])

        # 跨租户：另一租户查询无命中（索引前置过滤）
        other_username = "retrieval-other"
        _, other_tenant_id, _ = await create_user_with_tenant(container, username=other_username)
        other_headers = await _login(client, other_username, other_tenant_id)
        response = await client.post(
            "/v1/retrieval/test",
            json={"query": "retrieval", "dataset_ids": [str(dataset_id)], "top_k": 5},
            headers=other_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["items"] == []


async def test_service_param_overrides_and_echo() -> None:
    """测试台参数覆盖：rrf_k/权重/阈值生效并回显 applied_params。"""
    lexical = InMemoryLexicalIndex()
    lexical.upsert([_doc("c1", text="参数覆盖验证"), _doc("c2", text="参数权重验证")])
    service = RetrievalService(_enabled_settings(), dense_index=None, lexical_index=lexical)

    result = await service.search(
        tenant_id="t1",
        query="参数",
        query_vector=None,
        dataset_ids=None,
        acl_refs=None,
        authoritative_filter=_allow_all,
        rrf_k=1,
        channel_weights={"lexical": 0.0},
        min_relevance_threshold=999.0,
        per_channel_limit=1,
        top_k=3,
    )
    # 权重 0 → 融合结果为空；阈值 999 → 低相关
    assert result.items == []
    assert result.low_relevance is True
    assert result.applied_params["rrf_k"] == 1
    assert result.applied_params["channel_weights"] == {"lexical": 0.0}
    assert result.applied_params["min_relevance_threshold"] == 999.0
    assert result.applied_params["per_channel_limit"] == 1

    # 缺省回显配置值
    default_result = await service.search(
        tenant_id="t1",
        query="参数",
        query_vector=None,
        dataset_ids=None,
        acl_refs=None,
        authoritative_filter=_allow_all,
    )
    assert default_result.applied_params["rrf_k"] == RRF_K
    assert default_result.applied_params["min_relevance_threshold"] == 0.0


async def test_retrieval_api_accepts_tunable_params(container) -> None:
    username = "tuner"
    tenant_id, dataset_id, _target = await _setup_indexed_document(container, username)

    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, username, tenant_id)
        tuned = await client.post(
            "/v1/retrieval/test",
            json={
                "query": "world",
                "dataset_ids": [str(dataset_id)],
                "top_k": 5,
                "rrf_k": 10,
                "per_channel_limit": 5,
                "channel_weights": {"lexical": 2.0},
                "min_relevance_threshold": 0.0,
            },
            headers=headers,
        )
        assert tuned.status_code == 200, tuned.text
        params = tuned.json()["applied_params"]
        assert params["rrf_k"] == 10
        assert params["per_channel_limit"] == 5
        assert params["channel_weights"] == {"lexical": 2.0}
        assert params["candidate_limit"] == container.settings.retrieval.candidate_limit

        # 非法权重键 → 400；越界值 → 422（schema 校验）
        bad_key = await client.post(
            "/v1/retrieval/test",
            json={
                "query": "world",
                "dataset_ids": [str(dataset_id)],
                "channel_weights": {"sparse": 1.0},
            },
            headers=headers,
        )
        assert bad_key.status_code == 400
        bad_value = await client.post(
            "/v1/retrieval/test",
            json={
                "query": "world",
                "dataset_ids": [str(dataset_id)],
                "rrf_k": 0,
            },
            headers=headers,
        )
        assert bad_value.status_code == 422
