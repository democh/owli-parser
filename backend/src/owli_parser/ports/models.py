"""Model 端口：Generation / Embedding / Rerank 能力后端。

- 接口隔离：每类后端只实现自身支持的能力。
- 请求含模型 profile revision、输入和参数；context 含 tenant/actor、deadline、
  取消与预算；结果含 provider_request_id、实际模型、usage、finish_reason 与 warnings。
- 输出用 Pydantic 校验，不把外部 JSON 原样贯穿业务层。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class ModelCallContext:
    tenant_id: uuid.UUID
    actor_id: uuid.UUID
    purpose: str
    timeout_seconds: float
    cancellation: Any = None  # Callable[[], bool] | None
    budget_ref: str | None = None


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    model_profile_revision_id: uuid.UUID
    model_name: str
    messages: list[dict[str, str]]
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    stop: list[str] | None = None
    response_format_json: bool = False
    extra_parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GenerationUsage:
    prompt_tokens: int | None
    completion_tokens: int | None
    cost_estimate: str | None = None
    usage_status: Literal["reported", "unknown", "estimated"] = "reported"


@dataclass(frozen=True, slots=True)
class GenerationResult:
    text: str
    model_actual: str
    finish_reason: str | None
    provider_request_id: str | None
    usage: GenerationUsage | None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GenerationEvent:
    kind: Literal["delta", "done", "error"]
    delta_text: str = ""
    error_code: str | None = None
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class EmbeddingRequest:
    model_profile_revision_id: uuid.UUID
    model_name: str
    inputs: list[str]
    output_kind: Literal["dense", "sparse", "multivector"] = "dense"
    truncate: bool = False


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    dense: list[list[float]] | None
    sparse: list[dict[int, float]] | None
    dimensions: int | None
    model_actual: str
    usage: GenerationUsage | None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RerankRequest:
    model_profile_revision_id: uuid.UUID
    model_name: str
    query: str
    documents: list[str]
    top_n: int | None = None


@dataclass(frozen=True, slots=True)
class RerankResult:
    scores: list[float]
    indices: list[int]
    model_actual: str
    warnings: list[str] = field(default_factory=list)


class GenerationBackend(Protocol):
    async def generate(self, request: GenerationRequest, context: ModelCallContext) -> GenerationResult: ...

    def stream(
        self, request: GenerationRequest, context: ModelCallContext
    ) -> AsyncIterator[GenerationEvent]: ...


class EmbeddingBackend(Protocol):
    async def embed(self, request: EmbeddingRequest, context: ModelCallContext) -> EmbeddingResult: ...


class RerankBackend(Protocol):
    async def rerank(self, request: RerankRequest, context: ModelCallContext) -> RerankResult: ...


class ModelError(Exception):
    """模型调用错误。retryable 表示未开始有效输出的可恢复故障。"""

    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class ModelPolicyDenied(Exception):
    """外发策略/预算拒绝。不重试。"""
