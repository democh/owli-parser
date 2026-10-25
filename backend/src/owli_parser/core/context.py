"""执行上下文：ActorContext / TenantContext。

依赖注入传递不可变上下文；仓储租户操作必须显式接收 TenantContext，
禁止默认 tenant_id=None 表示全库。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum


class ActorKind(StrEnum):
    USER = "user"
    APPLICATION = "application"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class TenantContext:
    """租户作用域。仓储层所有租户资源操作必须显式接收此对象。"""

    tenant_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class ActorContext:
    """不可变执行主体上下文。

    - kind: 用户 / 外部应用 / 系统任务
    - subject_id: 用户 ID 或应用 ID（系统任务为 None 之外的占位）
    - tenant_id: 当前生效租户（平台级操作为 None）
    - credential_id: 本次请求使用的凭据 ID（Session ID / Token ID），用于撤销传播
    - auth_revision: 认证主体当前授权 revision，供副作用前复查
    - trace_id: 全链路追踪 ID
    """

    kind: ActorKind
    subject_id: uuid.UUID
    tenant_id: uuid.UUID | None
    credential_id: uuid.UUID | None
    auth_revision: int
    trace_id: str

    def require_tenant(self) -> TenantContext:
        if self.tenant_id is None:
            raise ValueError("当前上下文没有生效租户")
        return TenantContext(tenant_id=self.tenant_id)

    def with_tenant(self, tenant_id: uuid.UUID) -> ActorContext:
        from dataclasses import replace

        return replace(self, tenant_id=tenant_id)
