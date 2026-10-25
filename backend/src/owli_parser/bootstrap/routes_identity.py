"""认证、平台与成员路由。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from owli_parser.bootstrap.dependencies import (
    ActorState,
    get_container,
    require_platform_admin,
    require_tenant_admin,
    resolve_actor,
)
from owli_parser.contracts.api import RuntimeConfig
from owli_parser.core.errors import InvalidInputError
from owli_parser.modules.catalog.service import CatalogScope
from owli_parser.modules.identity.domain import TenantRole

router = APIRouter(tags=["identity"])


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(_Strict):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(_Strict):
    user_id: str
    username: str
    display_name: str
    is_platform_admin: bool
    csrf_token: str
    session_expires_at: str


class MeResponse(_Strict):
    user_id: str
    username: str
    display_name: str
    is_platform_admin: bool
    csrf_token: str  # 刷新后重新签发的 CSRF（仅存摘要，明文只出现在本响应）


class TenantEntry(_Strict):
    tenant_id: str
    name: str
    role: str
    status: str
    tenant_status: str


class ChangePasswordRequest(_Strict):
    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class PlatformTenantCreate(_Strict):
    name: str = Field(min_length=1, max_length=128)


class PlatformTenantPatch(_Strict):
    status: str


class PlatformUserCreate(_Strict):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    display_name: str | None = Field(default=None, max_length=128)
    is_platform_admin: bool = False


class PlatformUserPatch(_Strict):
    status: str


class MemberAdd(_Strict):
    username: str = Field(min_length=3, max_length=64)
    role: str


class MemberPatch(_Strict):
    role: str | None = None
    status: str | None = None


@router.get("/runtime-config")
async def runtime_config(request: Request) -> RuntimeConfig:
    container = get_container(request)
    return RuntimeConfig(
        app_name=container.settings.app.name,
        environment=container.settings.app.environment,
    )


@router.post("/auth/login")
async def login(request: Request, payload: LoginRequest, response: Response) -> LoginResponse:
    container = get_container(request)
    result = await container.identity.login(
        payload.username,
        payload.password,
        client_ip_hash=None,
        user_agent=request.headers.get("user-agent"),
    )
    settings = container.settings.auth.session
    response.set_cookie(
        key=settings.cookie_name,
        value=result.session_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        max_age=settings.absolute_ttl_seconds,
        path="/",
    )
    from datetime import timedelta

    expires_at = container.clock.now() + timedelta(seconds=settings.idle_ttl_seconds)
    return LoginResponse(
        user_id=str(result.user.id),
        username=result.user.username,
        display_name=result.user.display_name,
        is_platform_admin=result.user.is_platform_admin,
        csrf_token=result.csrf_token,
        session_expires_at=expires_at.isoformat(),
    )


@router.post("/auth/logout")
async def logout(request: Request, response: Response) -> dict[str, str]:
    container = get_container(request)
    token = request.cookies.get(container.settings.auth.session.cookie_name)
    if token:
        await container.identity.logout(token)
    response.delete_cookie(container.settings.auth.session.cookie_name, path="/")
    return {"status": "logged_out"}


@router.get("/auth/me")
async def me(state: ActorState = Depends(resolve_actor)) -> MeResponse:
    if state.is_application:
        raise InvalidInputError("应用主体请通过 /v1/capabilities 获取自身信息")
    request = state.request
    container = get_container(request)
    session_token = request.cookies.get(container.settings.auth.session.cookie_name)
    assert session_token is not None
    # 重新签发 CSRF：刷新页面后前端需要它执行变更请求（不放 localStorage）
    session, csrf_token = await container.identity.rotate_csrf(session_token)
    return MeResponse(
        user_id=str(session.user.id),
        username=session.user.username,
        display_name=session.user.display_name,
        is_platform_admin=session.user.is_platform_admin,
        csrf_token=csrf_token,
    )


@router.post("/auth/change-password")
async def change_password(
    payload: ChangePasswordRequest, state: ActorState = Depends(resolve_actor)
) -> dict[str, str]:
    if state.is_application:
        raise InvalidInputError("应用主体不能修改密码")
    container = get_container(state.request)
    await container.identity.change_password(
        state.actor.subject_id, payload.old_password, payload.new_password
    )
    return {"status": "password_changed"}


@router.get("/auth/tenants")
async def my_tenants(state: ActorState = Depends(resolve_actor)) -> list[TenantEntry]:
    if state.is_application:
        raise InvalidInputError("应用主体请使用 Token 绑定租户")
    container = get_container(state.request)
    tenants = await container.identity.list_tenants_for_user(state.actor.subject_id)
    return [
        TenantEntry(
            tenant_id=str(t.tenant_id),
            name=t.name,
            role=t.role,
            status=t.membership_status,
            tenant_status=t.tenant_status,
        )
        for t in tenants
    ]


# ---------------------------------------------------------------------------
# 平台管理（平台管理员）
# ---------------------------------------------------------------------------


@router.post("/platform/tenants", status_code=201)
async def create_tenant(
    payload: PlatformTenantCreate, state: ActorState = Depends(require_platform_admin)
) -> dict[str, str]:
    container = get_container(state.request)
    tenant_id = await container.identity.create_tenant(payload.name)
    return {"tenant_id": str(tenant_id)}


@router.patch("/platform/tenants/{tenant_id}")
async def patch_tenant(
    tenant_id: uuid.UUID, payload: PlatformTenantPatch, state: ActorState = Depends(require_platform_admin)
) -> dict[str, str]:
    container = get_container(state.request)
    await container.identity.set_tenant_status(tenant_id, payload.status)
    return {"status": "updated"}


@router.post("/platform/users", status_code=201)
async def create_user(
    payload: PlatformUserCreate, state: ActorState = Depends(require_platform_admin)
) -> dict[str, str]:
    container = get_container(state.request)
    user = await container.identity.create_user(
        username=payload.username,
        password=payload.password,
        display_name=payload.display_name,
        is_platform_admin=payload.is_platform_admin,
    )
    return {"user_id": str(user.id)}


@router.patch("/platform/users/{user_id}")
async def patch_user(
    user_id: uuid.UUID, payload: PlatformUserPatch, state: ActorState = Depends(require_platform_admin)
) -> dict[str, str]:
    container = get_container(state.request)
    await container.identity.set_user_status(user_id, payload.status)
    return {"status": "updated"}


# ---------------------------------------------------------------------------
# 租户成员（租户 admin）
# ---------------------------------------------------------------------------


@router.get("/settings/members")
async def list_members(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> list[dict[str, object]]:
    container = get_container(state.request)
    return await container.identity.list_members(scope.tenant_id)


@router.post("/members", status_code=201)
async def add_member(
    payload: MemberAdd,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, str]:
    container = get_container(state.request)
    if payload.role not in TenantRole.ALL:
        raise InvalidInputError("角色只允许 admin/editor/viewer")
    info = await container.identity.add_member(scope.tenant_id, payload.username, payload.role)
    return {"membership_id": str(info.membership_id)}


@router.patch("/members/{membership_id}")
async def patch_member(
    membership_id: uuid.UUID,
    payload: MemberPatch,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, str]:
    container = get_container(state.request)
    if payload.role is not None:
        await container.identity.update_member_role(scope.tenant_id, membership_id, payload.role)
    if payload.status is not None:
        await container.identity.set_member_status(scope.tenant_id, membership_id, payload.status)
    if payload.role is None and payload.status is None:
        raise InvalidInputError("没有需要修改的字段")
    return {"status": "updated"}


@router.delete("/members/{membership_id}")
async def delete_member(
    membership_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, str]:
    container = get_container(state.request)
    await container.identity.remove_member(scope.tenant_id, membership_id)
    return {"status": "removed"}
