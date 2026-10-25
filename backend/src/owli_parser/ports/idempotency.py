"""请求幂等端口。

- 唯一域 (tenant_id, actor_kind, actor_id, operation, key)。
- 同 key 同请求返回原资源；同 key 异请求返回 409。
- 重放前仍检查当前权限，不把旧响应缓存当作访问凭据。
- Token 明文不得进入幂等记录。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    id: uuid.UUID
    tenant_id: uuid.UUID | None
    actor_kind: str
    actor_id: uuid.UUID
    operation: str
    key: str
    request_hash: str
    response_ref: dict[str, Any]
    state: str  # in_progress | completed


class IdempotencyConflict(Exception):
    """同一幂等键对应了不同请求摘要。"""


class IdempotencyStore(Protocol):
    async def lookup(
        self,
        *,
        tenant_id: uuid.UUID | None,
        actor_kind: str,
        actor_id: uuid.UUID,
        operation: str,
        key: str,
    ) -> IdempotencyRecord | None: ...

    async def begin(
        self,
        *,
        tenant_id: uuid.UUID | None,
        actor_kind: str,
        actor_id: uuid.UUID,
        operation: str,
        key: str,
        request_hash: str,
        session: Any = None,
    ) -> IdempotencyRecord:
        """登记 in_progress 记录；同 key 异 request_hash 抛 IdempotencyConflict。"""
        ...

    async def complete(
        self,
        *,
        record_id: uuid.UUID,
        response_ref: dict[str, Any],
        session: Any = None,
    ) -> None: ...
