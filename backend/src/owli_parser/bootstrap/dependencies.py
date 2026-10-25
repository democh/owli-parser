"""FastAPI 依赖：认证解析、租户上下文与 CSRF/Origin 校验。

- 浏览器用户：服务端 Session（HttpOnly Cookie）+ X-Tenant-ID + CSRF 头。
- 外部应用：Authorization: Bearer 应用 Token；Token 绑定租户，无需 X-Tenant-ID，
  若提供且不一致直接拒绝（不悄悄选择权限更大的身份）。
- 同时携带 Session 与 Bearer 时拒绝歧义认证。
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import Depends, Request

from owli_parser.bootstrap.container import Container
from owli_parser.core.context import ActorContext, ActorKind
from owli_parser.core.errors import (
    ForbiddenError,
    NotAuthenticatedError,
    TenantMismatchError,
)
from owli_parser.modules.access.service import ApplicationPrincipal
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.service import CatalogScope

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
TENANT_HEADER = "x-tenant-id"


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


class ActorState:
    """依赖解析结果：认证主体 + 可选租户范围。"""

    def __init__(
        self,
        request: Request,
        actor: ActorContext,
        session_csrf_digest: str | None,
        application: ApplicationPrincipal | None,
    ) -> None:
        self.request = request
        self.actor = actor
        self.session_csrf_digest = session_csrf_digest
        self.application = application

    @property
    def is_user(self) -> bool:
        return self.actor.kind is ActorKind.USER

    @property
    def is_application(self) -> bool:
        return self.actor.kind is ActorKind.APPLICATION

    def require_platform_admin(self, is_admin: bool) -> None:
        if not is_admin:
            raise ForbiddenError("需要平台管理员权限")


async def resolve_actor(request: Request) -> ActorState:
    """解析认证主体并执行 CSRF/Origin 校验（用户会话的变更请求）。"""
    container = get_container(request)
    session_token = request.cookies.get(container.settings.auth.session.cookie_name)
    authorization = request.headers.get("authorization")
    if session_token and authorization:
        # 同时携带两种凭据：拒绝歧义认证
        raise NotAuthenticatedError("同时提供 Session 与 Bearer 凭据，认证歧义")

    if authorization:
        scheme, _, raw = authorization.partition(" ")
        if scheme.lower() != "bearer" or not raw:
            raise NotAuthenticatedError("Authorization 头格式不合法")
        principal = await container.access.authenticate_token(raw)
        tenant_header = request.headers.get(TENANT_HEADER)
        if tenant_header:
            try:
                header_tenant = uuid.UUID(tenant_header)
            except ValueError as exc:
                raise TenantMismatchError("X-Tenant-ID 不合法") from exc
            if header_tenant != principal.tenant_id:
                raise TenantMismatchError("X-Tenant-ID 与 Token 绑定租户不一致")
        actor = ActorContext(
            kind=ActorKind.APPLICATION,
            subject_id=principal.application_id,
            tenant_id=principal.tenant_id,
            credential_id=principal.token_id,
            auth_revision=principal.application_auth_revision,
            trace_id=getattr(request.state, "trace_id", ""),
        )
        # 供中间件在响应完成后写入脱敏调用记录（不存正文与凭据）
        request.state.application_usage = {
            "tenant_id": principal.tenant_id,
            "application_id": principal.application_id,
            "token_id": principal.token_id,
        }
        return ActorState(request, actor, None, principal)

    if session_token:
        session = await container.identity.authenticate_session(session_token)
        _verify_origin(request, container)
        if request.method not in _SAFE_METHODS:
            container.identity.verify_csrf(
                session.csrf_digest, request.headers.get(container.settings.auth.csrf_header_name)
            )
        actor = ActorContext(
            kind=ActorKind.USER,
            subject_id=session.user.id,
            tenant_id=None,
            credential_id=session.session_id,
            auth_revision=session.user.auth_revision,
            trace_id=getattr(request.state, "trace_id", ""),
        )
        request.state.session_user = session.user
        return ActorState(request, actor, session.csrf_digest, None)

    raise NotAuthenticatedError("未提供有效凭据")


def _verify_origin(request: Request, container: Any) -> None:
    """变更请求校验 Origin/Referer；与 public_base_url 或配置的 CORS 集合同源。

    没有 Origin/Referer（如 curl）时依赖 CSRF 头保护；有 Origin 时必须匹配。
    """
    if request.method in _SAFE_METHODS:
        return
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return
    allowed = {container.settings.app.public_base_url.rstrip("/")}
    allowed.update(o.rstrip("/") for o in container.settings.http.cors_allowed_origins)
    stripped = origin.rstrip("/")
    for candidate in allowed:
        if stripped.startswith(candidate):
            return
        # referer 为完整路径：比较 scheme+host 前缀
        from urllib.parse import urlparse

        parsed_candidate = urlparse(candidate)
        base = f"{parsed_candidate.scheme}://{parsed_candidate.netloc}"
        if stripped.startswith(base):
            return
    raise ForbiddenError("Origin 校验失败")


async def resolve_scope(state: ActorState = Depends(resolve_actor)) -> CatalogScope:
    """解析租户数据范围（CatalogScope）。

    应用主体在此集中执行 scope 强制（fail-closed）：未匹配任何规则的应用请求
    直接 403，避免新增路由默认对 Token 开放。"""
    container = get_container(state.request)
    actor = state.actor
    if state.is_application:
        assert state.application is not None
        _enforce_application_scope(state.request.method, state.request.url.path, state.application)
        actions = await container.access.granted_dataset_actions(state.application.application_id)
        return CatalogScope(
            tenant_id=state.application.tenant_id,
            kind=ActorKind.APPLICATION,
            actor_id=state.application.application_id,
            application_dataset_actions=actions,
        )
    # 用户：需要 X-Tenant-ID 且必须是本人有效 membership
    tenant_header = state.request.headers.get(TENANT_HEADER)
    if not tenant_header:
        raise TenantMismatchError("用户请求缺少 X-Tenant-ID")
    try:
        tenant_id = uuid.UUID(tenant_header)
    except ValueError as exc:
        raise TenantMismatchError("X-Tenant-ID 不合法") from exc
    membership = await container.identity.resolve_tenant_membership(actor.subject_id, tenant_id)
    user_ctx = UserAccessContext(user_id=actor.subject_id, role=Role(membership.role))
    return CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=actor.subject_id,
        user_ctx=user_ctx,
    )


async def require_tenant_admin(
    state: ActorState = Depends(resolve_actor), scope: CatalogScope = Depends(resolve_scope)
) -> CatalogScope:
    """租户 admin 或平台管理员（平台管理员不因此获得正文读取权，仅管理权）。"""
    if state.is_application:
        raise ForbiddenError("外部应用不能执行租户管理操作")
    user = getattr(state.request.state, "session_user", None)
    if user is not None and user.is_platform_admin:
        return scope
    if scope.user_ctx is None or scope.user_ctx.role is not Role.ADMIN:
        raise ForbiddenError("需要租户管理员权限")
    return scope


async def require_platform_admin(state: ActorState = Depends(resolve_actor)) -> ActorState:
    if state.is_application:
        raise ForbiddenError("外部应用不能执行平台管理操作")
    user = getattr(state.request, "state", None)
    session_user = getattr(user, "session_user", None) if user is not None else None
    if session_user is None or not session_user.is_platform_admin:
        raise ForbiddenError("需要平台管理员权限")
    return state


class IdempotencyContext:
    def __init__(self, key: str | None, state: ActorState) -> None:
        self.key = key
        self.state = state


# ---------------------------------------------------------------------------
# 应用 Token 的集中 scope 强制（文档第五部分 §4.4）
# ---------------------------------------------------------------------------

# (method 或 None 表示任意, 路径正则, 需要的 scope | "__user_only__" | tuple 附加)
_APP_SCOPE_RULES: list[tuple[str | None, str, str | tuple[str, ...]]] = [
    ("POST", r"^/v1/imports$", ("imports:create", "jobs:create")),
    ("POST", r"^/v1/imports/[^/]+/(cancel|retry)$", "imports:create"),
    ("GET", r"^/v1/imports", "imports:read"),
    ("POST", r"^/v1/uploads", "imports:create"),
    ("GET", r"^/v1/uploads", "imports:read"),
    ("POST", r"^/v1/parsing/jobs$", "jobs:create"),
    ("POST", r"^/v1/parsing/jobs/[^/]+/(cancel|retry)$", "jobs:control"),
    ("GET", r"^/v1/parsing/jobs", "jobs:read"),
    ("GET", r"^/v1/datasets", "datasets:read"),
    ("POST", r"^/v1/datasets", "__user_only__"),
    ("PATCH", r"^/v1/datasets", "__user_only__"),
    ("DELETE", r"^/v1/datasets", "__user_only__"),
    ("GET", r"^/v1/documents", "documents:read"),
    ("PATCH", r"^/v1/documents", "documents:write"),
    ("DELETE", r"^/v1/documents", "documents:delete"),
    ("PATCH", r"^/v1/chunks", "documents:write"),
    ("POST", r"^/v1/chunks/[^/]+/reembed$", "documents:write"),
    ("GET", r"^/v1/chunks", "documents:read"),
    ("GET", r"^/v1/artifacts", "artifacts:read"),
    ("POST", r"^/v1/retrieval/test$", "retrieval:query"),
    ("POST", r"^/v1/chat/runs$", "chat:invoke"),
    ("POST", r"^/v1/chat/runs/[^/]+/cancel$", "chat:invoke"),
    ("GET", r"^/v1/chat/runs", "chat:invoke"),
    ("GET", r"^/v1/capabilities$", ("",)),  # 应用可读自身能力（无额外 scope）
    (None, r"^/v1/review", "__user_only__"),
]


def _enforce_application_scope(method: str, path: str, principal: ApplicationPrincipal) -> None:
    from owli_parser.modules.access.service import require_scope

    for rule_method, pattern, required in _APP_SCOPE_RULES:
        if rule_method is not None and rule_method != method:
            continue
        if re.search(pattern, path) is None:
            continue
        if required == "__user_only__":
            raise ForbiddenError("该接口不面向外部应用")
        for scope in required if isinstance(required, tuple) else (required,):
            if scope:
                require_scope(principal, scope)
        return
    # fail-closed：未登记的应用可见接口一律拒绝
    raise ForbiddenError("该接口不在应用 Token 的 scope 注册表内")


async def idempotency_context(
    request: Request, state: ActorState = Depends(resolve_actor)
) -> IdempotencyContext:
    key = request.headers.get("idempotency-key") or None
    if key is not None and (len(key) > 128 or not key.strip()):
        from owli_parser.core.errors import InvalidInputError

        raise InvalidInputError("Idempotency-Key 不合法")
    return IdempotencyContext(key, state)
