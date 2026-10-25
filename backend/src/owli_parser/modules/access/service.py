"""access 应用服务：应用、Token 生命周期、授权决策。

- 每次入口鉴权读取权威 Token/应用状态，不依赖 TTL 正缓存承诺即时撤销。
- Token 只在创建成功时展示一次；重放只返回 token_id 与 secret_available=false。
- 最终权限取当前应用、Token、资源授权三者交集。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import cast

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from owli_parser.bootstrap.settings import TokensSection
from owli_parser.contracts.scopes import DATASET_ACTIONS, validate_scope_names
from owli_parser.core.clock import Clock, ensure_utc
from owli_parser.core.context import ActorKind
from owli_parser.core.crypto import constant_time_equals
from owli_parser.core.errors import (
    AlreadyExistsError,
    AppError,
    ConflictError,
    ErrorCode,
    ForbiddenError,
    InvalidInputError,
    NotAuthenticatedError,
    NotFoundError,
    ScopeRequiredError,
)
from owli_parser.infrastructure.telemetry import METRICS
from owli_parser.infrastructure.uow import UnitOfWork
from owli_parser.modules.access import domain
from owli_parser.modules.access.models import (
    ApplicationGrantRow,
    ApplicationRow,
    ApplicationTokenRow,
)
from owli_parser.modules.catalog.models import DatasetRow
from owli_parser.ports.audit import AuditRecorder


@dataclass(frozen=True, slots=True)
class ApplicationProfile:
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    description: str | None
    status: str
    allowed_scopes: list[str]
    auth_revision: int


@dataclass(frozen=True, slots=True)
class ApplicationPrincipal:
    """应用 Token 认证主体（不可与用户身份混用）。"""

    token_id: uuid.UUID
    application_id: uuid.UUID
    tenant_id: uuid.UUID
    scopes: frozenset[str]
    dataset_ids: frozenset[uuid.UUID]
    application_auth_revision: int
    token_name: str


@dataclass(frozen=True, slots=True)
class IssuedToken:
    token_id: uuid.UUID
    public_id: str
    raw_token: str  # 只在本次响应出现
    expires_at_epoch: int
    scopes: list[str]
    dataset_ids: list[str]


@dataclass(frozen=True, slots=True)
class GrantEntry:
    dataset_id: uuid.UUID
    actions: list[str] = field(default_factory=list)


class AccessService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        tokens_settings: TokensSection,
        pepper: str,
        audit: AuditRecorder,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._tokens = tokens_settings
        self._pepper = pepper
        self._audit = audit

    # ------------------------------------------------------------------
    # 应用管理（租户 admin）
    # ------------------------------------------------------------------

    async def create_application(
        self,
        tenant_id: uuid.UUID,
        *,
        name: str,
        description: str | None,
        allowed_scopes: list[str],
        created_by_id: uuid.UUID,
    ) -> ApplicationProfile:
        clean = name.strip()
        if not 1 <= len(clean) <= 128:
            raise InvalidInputError("应用名称长度需在 1-128 之间")
        unknown = validate_scope_names(allowed_scopes)
        if unknown:
            raise InvalidInputError(f"未知 scope: {', '.join(unknown)}")
        if not allowed_scopes:
            raise InvalidInputError("必须至少选择一个 scope")
        async with self._uow.begin() as session:
            row = ApplicationRow(
                tenant_id=tenant_id,
                name=clean,
                description=description,
                status="active",
                allowed_scopes=sorted(allowed_scopes),
                created_by_kind="user",
                created_by_id=created_by_id,
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise AlreadyExistsError("应用名称已存在") from exc
            return self._profile(row)

    async def list_applications(self, tenant_id: uuid.UUID) -> list[ApplicationProfile]:
        async with self._uow.begin() as session:
            rows = (
                await session.execute(
                    select(ApplicationRow)
                    .where(ApplicationRow.tenant_id == tenant_id)
                    .order_by(ApplicationRow.created_at)
                )
            ).scalars()
            return [self._profile(r) for r in rows]

    async def get_application(self, tenant_id: uuid.UUID, application_id: uuid.UUID) -> ApplicationProfile:
        async with self._uow.begin() as session:
            row = await self._get_app_row(session, tenant_id, application_id)
            return self._profile(row)

    async def update_application(
        self,
        tenant_id: uuid.UUID,
        application_id: uuid.UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        allowed_scopes: list[str] | None = None,
        status: str | None = None,
    ) -> ApplicationProfile:
        async with self._uow.begin() as session:
            row = await self._get_app_row(session, tenant_id, application_id)
            if name is not None:
                clean = name.strip()
                if not 1 <= len(clean) <= 128:
                    raise InvalidInputError("应用名称长度需在 1-128 之间")
                row.name = clean
            if description is not None:
                row.description = description
            if allowed_scopes is not None:
                unknown = validate_scope_names(allowed_scopes)
                if unknown:
                    raise InvalidInputError(f"未知 scope: {', '.join(unknown)}")
                row.allowed_scopes = sorted(allowed_scopes)
            if status is not None:
                if status not in ("active", "disabled"):
                    raise InvalidInputError("status 只允许 active/disabled")
                row.status = status
                if status == "disabled":
                    # 应用停用：立即撤销全部有效 Token
                    now = self._clock.now()
                    await session.execute(
                        update(ApplicationTokenRow)
                        .where(
                            ApplicationTokenRow.application_id == application_id,
                            ApplicationTokenRow.revoked_at.is_(None),
                        )
                        .values(revoked_at=now)
                    )
            row.auth_revision += 1
            return self._profile(row)

    # ------------------------------------------------------------------
    # 授权（grants）
    # ------------------------------------------------------------------

    async def replace_grants(
        self,
        tenant_id: uuid.UUID,
        application_id: uuid.UUID,
        grants: list[GrantEntry],
    ) -> None:
        """原子替换授权集合；新增/撤销递增应用 auth_revision。"""
        for grant in grants:
            invalid_actions = [a for a in grant.actions if a not in DATASET_ACTIONS]
            if invalid_actions:
                raise InvalidInputError(f"未知 actions: {', '.join(invalid_actions)}")
        async with self._uow.begin() as session:
            app = await self._get_app_row(session, tenant_id, application_id)
            dataset_ids = [g.dataset_id for g in grants]
            if dataset_ids:
                found = (
                    (
                        await session.execute(
                            select(DatasetRow.id).where(
                                DatasetRow.tenant_id == tenant_id,
                                DatasetRow.id.in_(dataset_ids),
                                DatasetRow.deleted_at.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                missing = set(dataset_ids) - set(found)
                if missing:
                    raise NotFoundError("授权目标数据集不存在或不可用")
            await session.execute(
                delete(ApplicationGrantRow).where(
                    ApplicationGrantRow.tenant_id == tenant_id,
                    ApplicationGrantRow.application_id == application_id,
                )
            )
            for grant in grants:
                session.add(
                    ApplicationGrantRow(
                        tenant_id=tenant_id,
                        application_id=application_id,
                        dataset_id=grant.dataset_id,
                        actions=sorted(set(grant.actions)),
                    )
                )
            app.auth_revision += 1

    async def list_grants(self, tenant_id: uuid.UUID, application_id: uuid.UUID) -> list[GrantEntry]:
        async with self._uow.begin() as session:
            await self._get_app_row(session, tenant_id, application_id)
            rows = (
                await session.execute(
                    select(ApplicationGrantRow).where(
                        ApplicationGrantRow.tenant_id == tenant_id,
                        ApplicationGrantRow.application_id == application_id,
                    )
                )
            ).scalars()
            return [GrantEntry(dataset_id=r.dataset_id, actions=list(r.actions or [])) for r in rows]

    async def granted_dataset_actions(self, application_id: uuid.UUID) -> dict[uuid.UUID, set[str]]:
        async with self._uow.read_only() as session:
            rows = (
                await session.execute(
                    select(ApplicationGrantRow).where(ApplicationGrantRow.application_id == application_id)
                )
            ).scalars()
            return {r.dataset_id: set(r.actions or []) for r in rows}

    # ------------------------------------------------------------------
    # Token 生命周期
    # ------------------------------------------------------------------

    async def issue_token(
        self,
        tenant_id: uuid.UUID,
        application_id: uuid.UUID,
        *,
        name: str,
        scopes: list[str],
        dataset_ids: list[str],
        expires_in_seconds: int | None,
        created_by_id: uuid.UUID,
    ) -> IssuedToken:
        clean = name.strip()
        if not 1 <= len(clean) <= 128:
            raise InvalidInputError("Token 名称长度需在 1-128 之间")
        ttl = domain.validate_ttl(
            expires_in_seconds or self._tokens.ttl_default_seconds,
            default_ttl=self._tokens.ttl_default_seconds,
            max_ttl=self._tokens.ttl_max_seconds,
        )
        async with self._uow.begin() as session:
            app = await self._get_app_row(session, tenant_id, application_id)
            if app.status != "active":
                raise ConflictError("应用已停用，不能签发 Token")
            unknown = validate_scope_names(scopes)
            if unknown:
                raise InvalidInputError(f"未知 scope: {', '.join(unknown)}")
            domain.validate_scopes_subset(scopes, list(app.allowed_scopes or []))
            grants = (
                (
                    await session.execute(
                        select(ApplicationGrantRow).where(
                            ApplicationGrantRow.tenant_id == tenant_id,
                            ApplicationGrantRow.application_id == application_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            granted_ids = {str(g.dataset_id) for g in grants}
            domain.validate_dataset_ids_subset(dataset_ids, granted_ids)

            raw_token, parsed = domain.generate_token(self._tokens.prefix)
            digest = domain.compute_digest(parsed.secret, self._pepper)
            now = self._clock.now()
            row = ApplicationTokenRow(
                public_id=parsed.public_id,
                application_id=application_id,
                tenant_id=tenant_id,
                name=clean,
                token_digest=digest,
                digest_key_version=1,
                scopes=sorted(set(scopes)),
                dataset_ids=sorted(set(dataset_ids)),
                expires_at=now + timedelta(seconds=ttl),
                created_by_kind="user",
                created_by_id=created_by_id,
            )
            session.add(row)
            await session.flush()
            await self._audit.record(
                action="access.token_issued",
                actor_kind=ActorKind.USER,
                actor_id=created_by_id,
                tenant_id=tenant_id,
                credential_id=None,
                resource_type="application_token",
                resource_id=str(row.id),
                trace_id="token-issue",
                decision="allow",
                session=session,
            )
            return IssuedToken(
                token_id=row.id,
                public_id=row.public_id,
                raw_token=raw_token,
                expires_at_epoch=int(row.expires_at.timestamp()),
                scopes=list(row.scopes),
                dataset_ids=list(row.dataset_ids),
            )

    async def list_tokens(self, tenant_id: uuid.UUID, application_id: uuid.UUID) -> list[dict[str, object]]:
        """列表只显示安全 public_id/名称、过期时间、状态、最近使用（不返回密钥）。"""
        async with self._uow.begin() as session:
            await self._get_app_row(session, tenant_id, application_id)
            rows = (
                await session.execute(
                    select(ApplicationTokenRow)
                    .where(
                        ApplicationTokenRow.tenant_id == tenant_id,
                        ApplicationTokenRow.application_id == application_id,
                    )
                    .order_by(ApplicationTokenRow.created_at)
                )
            ).scalars()
            now = self._clock.now()
            return [
                {
                    "token_id": str(r.id),
                    "public_id": r.public_id,
                    "name": r.name,
                    "scopes": list(r.scopes or []),
                    "dataset_ids": list(r.dataset_ids or []),
                    "expires_at": r.expires_at,
                    "revoked_at": r.revoked_at,
                    "last_used_at": r.last_used_at,
                    "status": (
                        "revoked"
                        if r.revoked_at is not None
                        else ("expired" if ensure_utc(r.expires_at) <= now else "active")
                    ),
                }
                for r in rows
            ]

    async def revoke_token(
        self,
        tenant_id: uuid.UUID,
        application_id: uuid.UUID,
        token_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> None:
        """幂等撤销；立即写权威库并审计。"""
        now = self._clock.now()
        async with self._uow.begin() as session:
            row = (
                await session.execute(
                    select(ApplicationTokenRow).where(
                        ApplicationTokenRow.id == token_id,
                        ApplicationTokenRow.tenant_id == tenant_id,
                        ApplicationTokenRow.application_id == application_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("Token 不存在")
            if row.revoked_at is None:
                row.revoked_at = now
                await self._audit.record(
                    action="access.token_revoked",
                    actor_kind=ActorKind.USER,
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    credential_id=None,
                    resource_type="application_token",
                    resource_id=str(token_id),
                    trace_id="token-revoke",
                    decision="allow",
                    session=session,
                )

    # ------------------------------------------------------------------
    # 认证与授权决策
    # ------------------------------------------------------------------

    async def authenticate_token(self, raw_token: str) -> ApplicationPrincipal:
        """每次入口鉴权读取权威 Token/应用状态；DB 不可用即 fail closed。

        拒绝原因仅按粗粒度分类计数（invalid/disabled）：聚合指标不区分
        "不存在/摘要不符/撤销/过期"，避免为枚举探测提供额外信号。
        """
        parsed = domain.parse_token(self._tokens.prefix, raw_token)
        now = self._clock.now()
        async with self._uow.begin() as session:
            row = (
                await session.execute(
                    select(ApplicationTokenRow).where(ApplicationTokenRow.public_id == parsed.public_id)
                )
            ).scalar_one_or_none()
            if row is None:
                # 不存在也使用统一错误与受限时序，避免枚举
                METRICS.inc("owli_token_rejected_total", {"reason": "invalid"})
                raise NotAuthenticatedError("Token 无效或已撤销")
            digest = domain.compute_digest(parsed.secret, self._pepper)
            if not constant_time_equals(digest, row.token_digest):
                METRICS.inc("owli_token_rejected_total", {"reason": "invalid"})
                raise NotAuthenticatedError("Token 无效或已撤销")
            if row.revoked_at is not None or ensure_utc(row.expires_at) <= now:
                METRICS.inc("owli_token_rejected_total", {"reason": "invalid"})
                raise NotAuthenticatedError("Token 无效或已撤销")
            app = (
                await session.execute(select(ApplicationRow).where(ApplicationRow.id == row.application_id))
            ).scalar_one_or_none()
            if app is None or app.status != "active":
                METRICS.inc("owli_token_rejected_total", {"reason": "disabled"})
                raise NotAuthenticatedError("应用已停用")
            # last_used_at 节流更新（60s），不影响吊销检查
            if row.last_used_at is None or (now - ensure_utc(row.last_used_at)).total_seconds() > 60:
                row.last_used_at = now
            return ApplicationPrincipal(
                token_id=row.id,
                application_id=app.id,
                tenant_id=app.tenant_id,
                scopes=frozenset(row.scopes or []),
                dataset_ids=frozenset(uuid.UUID(d) for d in (row.dataset_ids or [])),
                application_auth_revision=app.auth_revision,
                token_name=row.name,
            )

    async def authorization_snapshot(self, principal: ApplicationPrincipal) -> None:
        """副作用前复查：应用状态、Token 状态有效才放行；fail closed。"""
        async with self._uow.begin() as session:
            app = (
                await session.execute(
                    select(ApplicationRow).where(ApplicationRow.id == principal.application_id)
                )
            ).scalar_one_or_none()
            if app is None or app.status != "active":
                raise self._revoked()
            if app.auth_revision < principal.application_auth_revision:
                raise self._revoked()
            token = (
                await session.execute(
                    select(ApplicationTokenRow).where(
                        ApplicationTokenRow.id == principal.token_id,
                        ApplicationTokenRow.revoked_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if token is None or ensure_utc(token.expires_at) <= self._clock.now():
                raise self._revoked()

    @staticmethod
    def _revoked() -> AppError:
        return ForbiddenError("授权已被撤销", code=ErrorCode.AUTHORIZATION_REVOKED, http_status=403)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _get_app_row(
        self, session: object, tenant_id: uuid.UUID, application_id: uuid.UUID
    ) -> ApplicationRow:
        row = (
            await session.execute(  # type: ignore[attr-defined]
                select(ApplicationRow).where(
                    ApplicationRow.id == application_id,
                    ApplicationRow.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            # 资源不存在或属于其他租户统一按 404（避免枚举）
            raise NotFoundError("应用不存在")
        return cast(ApplicationRow, row)

    @staticmethod
    def _profile(row: ApplicationRow) -> ApplicationProfile:
        return ApplicationProfile(
            id=row.id,
            tenant_id=row.tenant_id,
            name=row.name,
            description=row.description,
            status=row.status,
            allowed_scopes=list(row.allowed_scopes or []),
            auth_revision=row.auth_revision,
        )


def require_scope(principal: ApplicationPrincipal, scope: str) -> None:
    if scope not in principal.scopes:
        raise ScopeRequiredError(f"缺少 scope: {scope}")


def require_scopes(principal: ApplicationPrincipal, *scopes: str) -> None:
    for scope in scopes:
        require_scope(principal, scope)


def require_grant_action(
    actions: dict[uuid.UUID, set[str]],
    principal: ApplicationPrincipal,
    dataset_id: uuid.UUID,
    action: str,
) -> None:
    """数据集 grant 校验：actions 必须包含指定动作，且数据集在 Token 集合内。"""
    if dataset_id not in principal.dataset_ids:
        raise ForbiddenError("Token 未授权该数据集")
    granted = actions.get(dataset_id, set())
    if action not in granted:
        raise ForbiddenError("应用未被授予该数据集操作权限")
