"""模型网关装配：按租户从"用途绑定 → 档案 → 连接"解析 embedding 提供者。

- 每次发布/重索引时解析（绑定变更无需重启进程）；
- API key 从密钥引用解析（引用形状已在连接创建时校验；此处仅 resolve）；
- httpx 共享 client 由 Container 持有（transport 可在测试注入 MockTransport）。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

from owli_parser.adapters.model_gateway import EmbeddingClient, GenerationClient, RerankClient
from owli_parser.modules.chat.service import GenerateResolver, GenerationHandle
from owli_parser.modules.retrieval.indexing import (
    EmbeddingProvider,
    EmbeddingProviderResolver,
)
from owli_parser.modules.retrieval.service import (
    RerankProvider,
    RerankProviderResolver,
)

if TYPE_CHECKING:
    from owli_parser.bootstrap.container import Container


def make_embedding_provider_resolver(container: Container) -> EmbeddingProviderResolver:
    async def resolve(tenant_id: uuid.UUID) -> EmbeddingProvider | None:
        binding = await container.configuration.get_model_binding(tenant_id, "embedding")
        if binding is None:
            return None
        profile = await container.configuration.get_model_profile(tenant_id, binding.primary_profile_id)
        if profile.state != "published" or not profile.embedding_dimensions:
            # 绑定校验已保证；防御性兜底不下发（dense 通道会以 capability_not_ready 失败）
            return None
        base_url, credential_ref = await container.configuration.get_model_connection_invocation(
            tenant_id, profile.connection_id
        )
        api_key = container.secrets.resolve(credential_ref) if credential_ref else None
        client = EmbeddingClient(
            base_url=base_url,
            model_name=profile.model_name,
            dimensions=profile.embedding_dimensions,
            api_key=api_key,
            client=container.model_http,
        )

        async def embed(texts: Sequence[str]) -> Sequence[Sequence[float]]:
            return await client.embed_batch(list(texts))

        return embed

    return resolve


async def _resolve_model_endpoint(
    container: Container, tenant_id: uuid.UUID, purpose: str
) -> tuple[str, str | None, str] | None:
    """绑定 → 档案 → 连接（base_url, api_key, model_name）；无绑定返回 None。"""
    binding = await container.configuration.get_model_binding(tenant_id, purpose)
    if binding is None:
        return None
    profile = await container.configuration.get_model_profile(tenant_id, binding.primary_profile_id)
    if profile.state != "published":
        return None
    base_url, credential_ref = await container.configuration.get_model_connection_invocation(
        tenant_id, profile.connection_id
    )
    api_key = container.secrets.resolve(credential_ref) if credential_ref else None
    return base_url, api_key, profile.model_name


def make_rerank_provider_resolver(container: Container) -> RerankProviderResolver:
    async def resolve(tenant_id: str) -> RerankProvider | None:
        resolved = await _resolve_model_endpoint(container, uuid.UUID(tenant_id), "rerank")
        if resolved is None:
            return None
        base_url, api_key, model_name = resolved
        client = RerankClient(
            base_url=base_url,
            model_name=model_name,
            api_key=api_key,
            client=container.model_http,
        )

        async def rerank(query: str, documents: Sequence[str]) -> Sequence[float]:
            return await client.rerank(query, list(documents))

        return rerank

    return resolve


def make_generation_resolver(container: Container) -> GenerateResolver:
    async def resolve(tenant_id: uuid.UUID) -> GenerationHandle | None:
        binding = await container.configuration.get_model_binding(tenant_id, "generation")
        if binding is None:
            return None
        resolved = await _resolve_model_endpoint(container, tenant_id, "generation")
        if resolved is None:
            return None
        base_url, api_key, model_name = resolved
        client = GenerationClient(
            base_url=base_url,
            model_name=model_name,
            api_key=api_key,
            client=container.model_http,
        )
        return GenerationHandle(stream=client.stream, budget=dict(binding.budget or {}))

    return resolve
