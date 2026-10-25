"""catalog 领域逻辑：数据集访问判定（用户角色与可见性矩阵）。

| 操作 | admin | editor | viewer |
| 读取 | 本租户全部 | tenant + 自己的 private | tenant + 自己已有的 private |
| 新建数据集 | 允许 | 允许 | 拒绝 |
| 上传/解析/编辑 | 允许 | 有访问权时允许 | 拒绝 |
| 改可见性/设置/删除数据集 | 允许 | 仅自己创建的 | 拒绝 |
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum


class DatasetVisibility(StrEnum):
    TENANT = "tenant"
    PRIVATE = "private"


class Role(StrEnum):
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


@dataclass(frozen=True, slots=True)
class UserAccessContext:
    """用户在本租户内的访问上下文（由 identity 解析 membership 得出）。"""

    user_id: uuid.UUID
    role: Role

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN


def can_read_dataset(
    ctx: UserAccessContext,
    *,
    visibility: str,
    owner_user_id: uuid.UUID,
) -> bool:
    """读取判定。

    - tenant 可见性：本租户有效成员均可读。
    - private：创建者与本租户 admin；其他成员不可读。
    """
    if ctx.is_admin:
        return True
    if visibility == DatasetVisibility.TENANT:
        return True
    if visibility == DatasetVisibility.PRIVATE:
        return owner_user_id == ctx.user_id
    return False


def can_write_dataset(ctx: UserAccessContext, *, visibility: str, owner_user_id: uuid.UUID) -> bool:
    """写判定：viewer 拒绝；editor 需有读权限；admin 允许。"""
    if ctx.role is Role.VIEWER:
        return False
    if ctx.is_admin:
        return True
    return can_read_dataset(ctx, visibility=visibility, owner_user_id=owner_user_id)


def can_manage_dataset(ctx: UserAccessContext, *, owner_user_id: uuid.UUID) -> bool:
    """改可见性/设置/删除数据集：admin 或创建者（editor 仅自己创建的）。"""
    return ctx.is_admin or owner_user_id == ctx.user_id


def can_create_dataset(ctx: UserAccessContext) -> bool:
    return ctx.role in (Role.ADMIN, Role.EDITOR)
