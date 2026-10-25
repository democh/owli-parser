"""embedding 客户端契约 + 用途绑定解析 + dense 端到端（MockTransport 注入）。

- 客户端契约：请求构造、响应守卫（条数/维度不符必须显式失败）、错误映射；
- resolver：无绑定 → None（dense 不可用语义保持）；有绑定 → 真实调用链路；
- 端到端：channels=[dense,lexical] + 绑定 → 发布写入 dense → 检索双通道命中。
mock 向量为确定性常量（验证链路，不评估语义相似度）。
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from owli_parser.adapters.model_gateway import EmbeddingClient
from owli_parser.bootstrap.model_wiring import make_embedding_provider_resolver
from owli_parser.core.context import ActorKind
from owli_parser.core.errors import ExternalServiceUnavailableError, UpstreamTimeoutError
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.service import CatalogScope
from tests.conftest import create_user_with_tenant

DIMENSIONS = 8
_CONSTANT_VECTOR = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def _scope(tenant_id: uuid.UUID, user_id: uuid.UUID) -> CatalogScope:
    return CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )


def _embeddings_response(texts: list[str], dimensions: int = DIMENSIONS) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "object": "list",
            "data": [
                {"object": "embedding", "index": i, "embedding": _CONSTANT_VECTOR[:dimensions]}
                for i in range(len(texts))
            ],
        },
    )


def _client(handler) -> EmbeddingClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return EmbeddingClient(
        base_url="http://models.internal:8000/v1",
        model_name="bge-m3",
        dimensions=DIMENSIONS,
        api_key="sk-test",
        client=http,
    )


async def test_embedding_client_contract() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        return _embeddings_response(body["input"])

    client = _client(handler)
    vectors = await client.embed_batch(["第一段", "第二段"])
    assert len(vectors) == 2 and len(vectors[0]) == DIMENSIONS
    assert seen[0].url.path == "/v1/embeddings"
    assert seen[0].headers["authorization"] == "Bearer sk-test"
    assert json.loads(seen[0].content)["model"] == "bge-m3"

    # 空批不调用上游
    assert await client.embed_batch([]) == []
    assert len(seen) == 1


async def test_embedding_client_guards_and_error_mapping() -> None:
    # 条数不符 → 明确失败
    mismatched = _client(lambda request: _embeddings_response(["少一条"]))
    with pytest.raises(ExternalServiceUnavailableError, match="条数不符"):
        await mismatched.embed_batch(["a", "b"])

    # 维度不符 → 拒绝（不截断不补齐）
    wrong_dim = _client(lambda request: _embeddings_response(["x"], dimensions=DIMENSIONS - 1))
    with pytest.raises(ExternalServiceUnavailableError, match="维度不符"):
        await wrong_dim.embed_batch(["a"])

    # HTTP 500 → ExternalServiceUnavailableError（带状态码）
    failing = _client(lambda request: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(ExternalServiceUnavailableError, match="500"):
        await failing.embed_batch(["a"])

    # 超时 → UpstreamTimeoutError
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    timing_out = _client(timeout_handler)
    with pytest.raises(UpstreamTimeoutError):
        await timing_out.embed_batch(["a"])

    # 非法 JSON → 明确失败
    broken = _client(lambda request: httpx.Response(200, content=b"not-json"))
    with pytest.raises(ExternalServiceUnavailableError, match="data"):
        await broken.embed_batch(["a"])


async def _embedding_ready_tenant(container, username: str):
    """准备：published 连接 + embedding 档案 + 绑定 + MockTransport；返回 (user, tenant, calls)。"""
    user_id, tenant_id, _ = await create_user_with_tenant(container, username=username)
    connection = await container.configuration.create_model_connection(
        tenant_id,
        name=f"conn-{username}",
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
        model_name="bge-m3",
        capabilities=["embedding"],
        context_window=None,
        tokenizer_ref=None,
        max_input_tokens=None,
        max_output_tokens=None,
        supported_parameters=[],
        pricing_ref=None,
        embedding_dimensions=DIMENSIONS,
        embedding_normalization="l2",
        embedding_distance="cosine",
        output_kinds=[],
        actor_id=user_id,
    )
    await container.configuration.upsert_model_binding(
        tenant_id,
        "embedding",
        primary_profile_id=profile.id,
        fallback_profile_ids=[],
        expected_revision=None,
        actor_id=user_id,
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["model"])
        return _embeddings_response(body["input"])

    container.embedding_transport = httpx.MockTransport(handler)
    return user_id, tenant_id, calls


async def test_resolver_none_without_binding_then_provider_after_binding(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="emb-resolve")
    resolver = make_embedding_provider_resolver(container)
    assert await resolver(tenant_id) is None  # 无绑定 → dense 不可用语义保持

    _, bound_tenant, calls = await _embedding_ready_tenant(container, "emb-resolve2")
    provider = await resolver(bound_tenant)
    assert provider is not None
    vectors = await provider(["一段文本"])
    assert len(vectors) == 1 and len(vectors[0]) == DIMENSIONS
    assert calls  # 经 mock transport 真实出站
    del user_id


async def test_dense_end_to_end_with_binding(container) -> None:
    """channels=[dense,lexical] + 绑定 → 发布写入 dense → 检索双通道。"""
    user_id, tenant_id, calls = await _embedding_ready_tenant(container, "emb-e2e")
    scope = _scope(tenant_id, user_id)
    dataset = await container.catalog.create_dataset(
        scope, name="ds-emb-e2e", description=None, visibility="tenant"
    )
    container.settings.retrieval.enabled = True
    container.settings.retrieval.channels = ["dense", "lexical"]

    body = "第一章 总则\n本章说明检索增强的索引写入链路。\n第二章 验收\n".encode()
    upload = await container.uploads.init_upload(
        scope, dataset_id=dataset.id, filename="doc.txt", declared_total_bytes=len(body)
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
        filename="doc.txt",
    )
    pipeline = container.submit_parsing.compile_default_pipeline()
    await container.run_parsing_job._execute(result.job_id, "worker-test", pipeline)

    job = await container.jobs.get_job(tenant_id, result.job_id)
    assert job.state == "DONE", job.error
    manifest = await container.knowledge.latest_manifest(result.version_id)
    assert manifest is not None and manifest.required_channels == ["dense", "lexical"]
    assert calls  # 发布阶段确实调用了 embedding 服务

    # 检索：查询向量与全部块向量一致（常量 mock）→ dense+lexical 双通道命中
    async def allow_all(candidates) -> set[str]:
        return {hit.id for hit in candidates}

    search = await container.retrieval.search(
        tenant_id=str(tenant_id),
        query="检索",
        query_vector=list(_CONSTANT_VECTOR),
        dataset_ids=[str(dataset.id)],
        acl_refs=[f"dataset:{dataset.id}"],
        authoritative_filter=allow_all,
    )
    assert search.channels_used == ["dense", "lexical"]
    assert search.items, "双通道应返回命中"
