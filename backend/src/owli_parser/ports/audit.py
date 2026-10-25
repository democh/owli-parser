"""Audit 端口：结构化审计记录。

audit_logs 记录 actor、资源版本、动作、理由和 trace_id；
避免记录正文、JWT、密钥、预签名 URL。
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from owli_parser.core.context import ActorKind


class AuditRecorder(Protocol):
    async def record(
        self,
        *,
        action: str,
        actor_kind: ActorKind,
        actor_id: uuid.UUID | None,
        tenant_id: uuid.UUID | None,
        credential_id: uuid.UUID | None,
        resource_type: str,
        resource_id: str | None,
        trace_id: str,
        decision: str | None = None,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
        session: Any = None,
    ) -> None: ...
