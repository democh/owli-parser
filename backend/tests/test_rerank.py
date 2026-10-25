"""rerank 接入测试：客户端契约、服务级启用规则、正文缺失降级与端到端重排。

- rerank_model_ref 非空即启用；无 rerank 用途绑定 → 明确失败（不静默降级）；
- 客户端响应守卫：index 覆盖 [0,n) 恰好一次（缺项/越界/重复均显式失败）；
- rerank 仅作用于权威校验之后的候选；正文缺失候选不评分、保持原序置后。
"""

from __future__ import annotations

import json

import httpx
import pytest

from owli_parser.adapters.model_gateway import RerankClient
from owli_parser.bootstrap.settings import RetrievalSection
from owli_parser.core.errors import (
    CapabilityNotReadyError,
    ExternalServiceUnavailableError,
    UpstreamTimeoutError,
)
from owli_parser.modules.retrieval.domain import FusedHit
from owli_parser.modules.retrieval.service import RetrievalService


def _client(handler) -> RerankClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RerankClient(
        base_url="http://models.internal:8000/v1",
        model_name="bge-reranker-v2",
        api_key="sk-test",
        client=http,
    )


def _results_response(pairs: list[tuple[int, float]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"results": [{"index": index, "relevance_score": score} for index, score in pairs]},
    )


async def test_rerank_client_contract_and_guards() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        # 故意乱序返回，客户端须按 index 归位
        return _results_response([(1, 0.9), (0, 0.1), (2, 0.5)])

    client = _client(handler)
    scores = await client.rerank("查询词", ["a", "b", "c"])
    assert scores == [0.1, 0.9, 0.5]
    body = json.loads(seen[0].content)
    assert body["model"] == "bge-reranker-v2" and body["query"] == "查询词"
    assert body["documents"] == ["a", "b", "c"]
    assert seen[0].url.path == "/v1/rerank"

    assert await client.rerank("q", []) == []  # 空文档不调用

    # 缺 index → 响应不完整（不猜测）
    with pytest.raises(ExternalServiceUnavailableError, match="不完整"):
        await _client(lambda request: _results_response([(0, 0.5)])).rerank("q", ["a", "b"])
    # 越界 index
    with pytest.raises(ExternalServiceUnavailableError, match="越界"):
        await _client(lambda request: _results_response([(0, 0.5), (2, 0.5)])).rerank("q", ["a", "b"])
    # 重复 index
    with pytest.raises(ExternalServiceUnavailableError, match="重复"):
        await _client(lambda request: _results_response([(0, 0.5), (0, 0.6)])).rerank("q", ["a", "b"])
    # HTTP 500
    with pytest.raises(ExternalServiceUnavailableError, match="500"):
        await _client(lambda request: httpx.Response(500, json={})).rerank("q", ["a"])

    # 超时
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("t")

    with pytest.raises(UpstreamTimeoutError):
        await _client(timeout_handler).rerank("q", ["a"])


def _hits(ids: list[str]) -> list[FusedHit]:
    return [FusedHit(id=chip, rrf_score=1.0) for chip in ids]


async def _service(
    *,
    resolver,
    text_loader=None,
    rerank_model_ref: str | None = "bge-reranker-v2",
) -> RetrievalService:
    settings = RetrievalSection(rerank_model_ref=rerank_model_ref, channels=["lexical"])
    return RetrievalService(
        settings,
        dense_index=None,
        lexical_index=None,
        rerank_provider_resolver=resolver,
        text_loader=text_loader,
    )


async def test_disabled_rerank_never_calls_provider() -> None:
    calls: list[str] = []

    async def resolver(tenant_id: str):
        calls.append(tenant_id)
        return lambda query, docs: _scores([0.0] * len(docs))

    service = await _service(resolver=resolver, rerank_model_ref=None)
    warnings: list[str] = []
    hits, scores, applied = await service._rerank(
        _hits(["a", "b"]), tenant_id="t1", query="q", warnings=warnings
    )
    assert [hit.id for hit in hits] == ["a", "b"]
    assert scores == {} and applied is False and calls == []


async def test_enabled_rerank_without_binding_fails() -> None:
    async def resolver(tenant_id: str):
        return None

    service = await _service(resolver=resolver)
    with pytest.raises(CapabilityNotReadyError, match="缺少 rerank 用途绑定"):
        await service._rerank(_hits(["a"]), tenant_id="t1", query="q", warnings=[])


async def test_missing_text_candidates_degrade_honestly() -> None:
    """正文缺失候选不评分：保持原相对顺序置后并记 warning（不伪造分数）。"""

    async def resolver(tenant_id: str):
        async def provider(query: str, documents: list[str]) -> list[float]:
            return [0.9 if doc == "CCC" else 0.1 for doc in documents]

        return provider

    async def text_loader(tenant_id: str, ids: list[str]) -> dict[str, str]:
        return {"a": "AAA", "c": "CCC"}  # b 缺失

    service = await _service(resolver=resolver, text_loader=text_loader)
    warnings: list[str] = []
    hits, scores, applied = await service._rerank(
        _hits(["c", "b", "a"]), tenant_id="t1", query="q", warnings=warnings
    )
    # 有分：a(0.1)/c(0.9) 按分降序 → c, a；无分 b 置后
    assert [hit.id for hit in hits] == ["c", "a", "b"]
    assert scores == {"a": 0.1, "c": 0.9}
    assert applied is True
    assert any("1 个候选缺少正文" in message for message in warnings)


async def _scores(values: list[float]) -> list[float]:
    return values


async def test_rerank_end_to_end_reorders_results(container) -> None:
    """端到端：真实解析 + lexical 索引 + rerank 绑定（MockTransport）→ 顺序被重排。"""
    from owli_parser.core.context import ActorKind
    from owli_parser.modules.catalog.domain import Role, UserAccessContext
    from owli_parser.modules.catalog.service import CatalogScope
    from tests.conftest import create_user_with_tenant

    user_id, tenant_id, _ = await create_user_with_tenant(container, username="rr-e2e")
    scope = CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )
    dataset = await container.catalog.create_dataset(
        scope, name="ds-rr", description=None, visibility="tenant"
    )
    # 必须在解析（发布写入）之前启用检索与通道，否则索引为空
    container.settings.retrieval.enabled = True
    container.settings.retrieval.channels = ["lexical"]
    container.settings.retrieval.rerank_model_ref = "bge-reranker-v2"
    body = "检索增强生成介绍。\n检索质量评估方法。\n数据库索引原理。\n".encode()
    upload = await container.uploads.init_upload(
        scope, dataset_id=dataset.id, filename="rr.txt", declared_total_bytes=len(body)
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
        filename="rr.txt",
    )
    pipeline = container.submit_parsing.compile_default_pipeline()
    await container.run_parsing_job._execute(result.job_id, "worker-test", pipeline)
    job = await container.jobs.get_job(tenant_id, result.job_id)
    assert job.state == "DONE", job.error

    # rerank 绑定（published 连接 + rerank 能力档案）
    connection = await container.configuration.create_model_connection(
        tenant_id,
        name="conn-rr",
        protocol="openai_compatible",
        base_url="http://models.internal:8000/v1",
        vendor_label=None,
        region=None,
        credential_ref=None,
        actor_id=user_id,
    )
    await container.configuration.publish_model_connection(tenant_id, connection.id)
    profile = await container.configuration.create_model_profile(
        tenant_id,
        connection_id=connection.id,
        model_name="bge-reranker-v2",
        capabilities=["rerank"],
        context_window=None,
        tokenizer_ref=None,
        max_input_tokens=None,
        max_output_tokens=None,
        supported_parameters=[],
        pricing_ref=None,
        embedding_dimensions=None,
        embedding_normalization=None,
        embedding_distance=None,
        output_kinds=[],
        actor_id=user_id,
    )
    await container.configuration.upsert_model_binding(
        tenant_id,
        "rerank",
        primary_profile_id=profile.id,
        fallback_profile_ids=[],
        expected_revision=None,
        actor_id=user_id,
    )

    rerank_calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body_json = json.loads(request.content)
        rerank_calls.append(body_json)
        docs = body_json["documents"]
        # 给"数据库索引原理"最高分（反转 lexical 直觉顺序）
        pairs = [(index, 0.99 if "数据库" in doc else 0.1) for index, doc in enumerate(docs)]
        return httpx.Response(200, json={"results": [{"index": i, "relevance_score": s} for i, s in pairs]})

    container.embedding_transport = httpx.MockTransport(handler)

    async def allow_all(candidates) -> set[str]:
        return {hit.id for hit in candidates}

    search = await container.retrieval.search(
        tenant_id=str(tenant_id),
        query="检索",
        query_vector=None,
        dataset_ids=[str(dataset.id)],
        acl_refs=[f"dataset:{dataset.id}"],
        authoritative_filter=allow_all,
    )
    assert search.rerank_applied is True
    assert rerank_calls and len(rerank_calls[0]["documents"]) == len(search.items)
    assert search.items[0].rerank_score == pytest.approx(0.99)
    assert "数据库" in rerank_calls[0]["documents"][0]  # 重排后首位为最高分文档
    assert search.applied_params["rerank_applied"] is True
