"""identity 应用服务：认证、会话、租户与成员管理。

- 登录校验来源并限流；失败写入审计（不记录密码）。
- Session 验证每次读取权威库，支持即时撤销；滑动续期受 absolute TTL 约束。
- 成员角色固定 admin/editor/viewer；不实现部门树与自定义角色。
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from owli_parser.bootstrap.settings import AuthSection
from owli_parser.core.clock import Clock, ensure_utc
from owli_parser.core.context import ActorKind, TenantContext
from owli_parser.core.crypto import constant_time_equals
from owli_parser.core.errors import (
    AlreadyExistsError,
    ConflictError,
    ForbiddenError,
    InvalidInputError,
    NotAuthenticatedError,
    NotFoundError,
    RateLimitedError,
    SessionExpiredError,
    ValidationFailedError,
)
from owli_parser.infrastructure.crosscut_models import AuditLogRow
from owli_parser.infrastructure.uow import UnitOfWork
from owli_parser.modules.identity import domain
from owli_parser.modules.identity.models import (
    MembershipRow,
    TenantRow,
    UserRow,
    UserSessionRow,
)
from owli_parser.ports.audit import AuditRecorder


@dataclass(frozen=True, slots=True)
class UserProfile:
    id: uuid.UUID
    username: str
    display_name: str
    is_platform_admin: bool
    status: str
    auth_revision: int


@dataclass(frozen=True, slots=True)
class TenantInfo:
    tenant_id: uuid.UUID
    name: str
    tenant_status: str
    role: str
    membership_id: uuid.UUID
    membership_status: str


@dataclass(frozen=True, slots=True)
class LoginResult:
    session_id: uuid.UUID
    session_token: str
    csrf_token: str
    user: UserProfile


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    session_id: uuid.UUID
    user: UserProfile
    csrf_digest: str


@dataclass(frozen=True, slots=True)
class WsTicketClaims:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    session_id: uuid.UUID
    expires_at_epoch: int


class IdentityService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        auth_settings: AuthSection,
        pepper: str,
        audit: AuditRecorder,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._auth = auth_settings
        self._pepper = pepper
        self._audit = audit
        # 已用 WS ticket（一次性）：进程内集合。首期单副本合并部署；多副本时
        # 需换共享存储（如 Valkey SETNX），此处不得作为长期方案。
        self._used_ws_tickets: dict[str, int] = {}

    # ------------------------------------------------------------------
    # WS 票据（浏览器事件连接）：租户绑定、短时、一次性
    # ------------------------------------------------------------------

    def issue_ws_ticket(
        self, user_id: uuid.UUID, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> tuple[str, int]:
        """签发 HMAC 签名的一次性 WS ticket；禁止把长期凭据放入 URL。"""
        ttl = self._auth.ws_ticket_ttl_seconds
        expires = int(time.time()) + ttl
        payload = f"{user_id}.{tenant_id}.{session_id}.{expires}"
        sig = hmac.new(f"ws-ticket:{self._pepper}".encode(), payload.encode(), hashlib.sha256).hexdigest()
        # 定期清理过期的已用票据，避免无界增长
        now = int(time.time())
        self._used_ws_tickets = {key: exp for key, exp in self._used_ws_tickets.items() if exp > now}
        return f"{payload}.{sig}", ttl

    def verify_ws_ticket(self, ticket: str) -> WsTicketClaims:
        """校验签名、有效期与一次性；失败一律 NotAuthenticatedError。"""
        if not ticket or len(ticket) > 256:
            raise NotAuthenticatedError("WS 票据无效")
        parts = ticket.rsplit(".", 1)
        if len(parts) != 2:
            raise NotAuthenticatedError("WS 票据无效")
        payload, provided_sig = parts
        expected_sig = hmac.new(
            f"ws-ticket:{self._pepper}".encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if not constant_time_equals(provided_sig, expected_sig):
            raise NotAuthenticatedError("WS 票据无效")
        fields = payload.split(".")
        if len(fields) != 4:
            raise NotAuthenticatedError("WS 票据无效")
        try:
            user_id = uuid.UUID(fields[0])
            tenant_id = uuid.UUID(fields[1])
            session_id = uuid.UUID(fields[2])
            expires = int(fields[3])
        except ValueError as exc:
            raise NotAuthenticatedError("WS 票据无效") from exc
        now = int(time.time())
        if expires <= now:
            raise NotAuthenticatedError("WS 票据已过期")
        if ticket in self._used_ws_tickets:
            raise NotAuthenticatedError("WS 票据已被使用")
        self._used_ws_tickets[ticket] = expires
        return WsTicketClaims(
            user_id=user_id,
            tenant_id=tenant_id,
            session_id=session_id,
            expires_at_epoch=expires,
        )

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------

    async def login(
        self,
        username_raw: str,
        password: str,
        *,
        client_ip_hash: str | None = None,
        user_agent: str | None = None,
    ) -> LoginResult:
        username = domain.normalize_username(username_raw)
        await self._enforce_login_rate_limit(username)
        now = self._clock.now()
        rejected = False

        async with self._uow.begin() as session:
            user = (
                await session.execute(select(UserRow).where(UserRow.username == username))
            ).scalar_one_or_none()
            if (
                user is None
                or user.status != "active"
                or not domain.verify_password(user.password_hash, password)
            ):
                # 失败审计必须独立提交（登录事务会回滚；限流依赖该记录）
                rejected = True
            else:
                session_token, csrf_token = domain.issue_session_credentials()
                token_digest = domain.compute_digest(session_token, self._pepper)
                csrf_digest = domain.compute_digest(csrf_token, self._pepper)
                row = UserSessionRow(
                    user_id=user.id,
                    token_digest=token_digest,
                    digest_key_version=domain.DIGEST_KEY_VERSION,
                    csrf_digest=csrf_digest,
                    last_seen_at=now,
                    idle_expires_at=now + timedelta(seconds=self._auth.session.idle_ttl_seconds),
                    absolute_expires_at=now + timedelta(seconds=self._auth.session.absolute_ttl_seconds),
                    client_ip_hash=client_ip_hash,
                    user_agent=(user_agent or "")[:256] or None,
                )
                session.add(row)
                await session.flush()
                await self._audit.record(
                    action="auth.login",
                    actor_kind=ActorKind.USER,
                    actor_id=user.id,
                    tenant_id=None,
                    credential_id=row.id,
                    resource_type="session",
                    resource_id=str(row.id),
                    trace_id="login",
                    decision="allow",
                    session=session,
                )
                return LoginResult(
                    session_id=row.id,
                    session_token=session_token,
                    csrf_token=csrf_token,
                    user=self._profile(user),
                )
        if rejected:
            await self._audit.record(
                action="auth.login_failed",
                actor_kind=ActorKind.USER,
                actor_id=None,
                tenant_id=None,
                credential_id=None,
                resource_type="user",
                resource_id=username,
                trace_id="login",
                decision="deny",
            )
            raise NotAuthenticatedError("用户名或密码错误")
        raise NotAuthenticatedError("用户名或密码错误")

    async def logout(self, session_token: str) -> None:
        """幂等登出：撤销对应会话。"""
        token_digest = domain.compute_digest(session_token, self._pepper)
        now = self._clock.now()
        async with self._uow.begin() as session:
            row = (
                await session.execute(
                    select(UserSessionRow).where(UserSessionRow.token_digest == token_digest)
                )
            ).scalar_one_or_none()
            if row is not None and row.revoked_at is None:
                row.revoked_at = now

    async def authenticate_session(self, session_token: str) -> AuthenticatedSession:
        """验证会话并滑动续期；已撤销/过期/用户停用一律拒绝。"""
        token_digest = domain.compute_digest(session_token, self._pepper)
        now = self._clock.now()
        async with self._uow.begin() as session:
            row = (
                await session.execute(
                    select(UserSessionRow).where(UserSessionRow.token_digest == token_digest)
                )
            ).scalar_one_or_none()
            if row is None or row.revoked_at is not None:
                raise NotAuthenticatedError("会话无效或已撤销")
            if now >= ensure_utc(row.absolute_expires_at):
                raise SessionExpiredError("会话已过期")
            if now >= ensure_utc(row.idle_expires_at):
                row.revoked_at = now
                raise SessionExpiredError("会话空闲超时")
            user = (
                await session.execute(select(UserRow).where(UserRow.id == row.user_id))
            ).scalar_one_or_none()
            if user is None or user.status != "active":
                row.revoked_at = now
                raise NotAuthenticatedError("账号不可用")
            # 滑动续期：idle 到期时间顺延，但不超过 absolute
            row.last_seen_at = now
            new_idle = now + timedelta(seconds=self._auth.session.idle_ttl_seconds)
            row.idle_expires_at = min(new_idle, ensure_utc(row.absolute_expires_at))
            return AuthenticatedSession(
                session_id=row.id, user=self._profile(user), csrf_digest=row.csrf_digest
            )

    def verify_csrf(self, csrf_digest: str, provided_token: str | None) -> None:
        if not provided_token:
            raise ForbiddenError("缺少 CSRF 令牌")
        digest = domain.compute_digest(provided_token, self._pepper)
        if not constant_time_equals(digest, csrf_digest):
            raise ForbiddenError("CSRF 校验失败")

    async def rotate_csrf(self, session_token: str) -> tuple[AuthenticatedSession, str]:
        """重新生成 CSRF 令牌（刷新页面后前端经 /auth/me 重新获取）。

        返回 (会话信息, 明文 csrf_token)；只存摘要，明文仅本次响应。
        """
        token_digest = domain.compute_digest(session_token, self._pepper)
        csrf_token = domain.new_csrf_token()
        csrf_digest = domain.compute_digest(csrf_token, self._pepper)
        async with self._uow.begin() as session:
            row = (
                await session.execute(
                    select(UserSessionRow).where(UserSessionRow.token_digest == token_digest)
                )
            ).scalar_one_or_none()
            if row is None or row.revoked_at is not None:
                raise NotAuthenticatedError("会话无效或已撤销")
            row.csrf_digest = csrf_digest
        authenticated = await self.authenticate_session(session_token)
        return authenticated, csrf_token

    async def revoke_all_sessions(self, user_id: uuid.UUID, session: object = None) -> None:
        now = self._clock.now()
        stmt = (
            update(UserSessionRow)
            .where(UserSessionRow.user_id == user_id, UserSessionRow.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        if session is not None:
            await session.execute(stmt)  # type: ignore[attr-defined]
            return
        async with self._uow.begin() as own:
            await own.execute(stmt)

    async def change_password(self, user_id: uuid.UUID, old_password: str, new_password: str) -> None:
        domain.validate_password_policy(
            new_password,
            min_length=self._auth.password.min_length,
            max_length=self._auth.password.max_length,
        )
        async with self._uow.begin() as session:
            user = (await session.execute(select(UserRow).where(UserRow.id == user_id))).scalar_one_or_none()
            if user is None or user.status != "active":
                raise NotAuthenticatedError("账号不可用")
            if not domain.verify_password(user.password_hash, old_password):
                raise NotAuthenticatedError("原密码不正确")
            user.password_hash = domain.hash_password(new_password)
            user.auth_revision += 1
            now = self._clock.now()
            await session.execute(
                update(UserSessionRow)
                .where(UserSessionRow.user_id == user_id, UserSessionRow.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            await self._audit.record(
                action="auth.change_password",
                actor_kind=ActorKind.USER,
                actor_id=user_id,
                tenant_id=None,
                credential_id=None,
                resource_type="user",
                resource_id=str(user_id),
                trace_id="change-password",
                decision="allow",
                session=session,
            )

    # ------------------------------------------------------------------
    # 用户与租户
    # ------------------------------------------------------------------

    async def bootstrap_admin(
        self, username: str, password: str, display_name: str | None = None
    ) -> UserProfile:
        """创建平台管理员。已存在同名用户时不重置密码（幂等）。"""
        normalized = domain.normalize_username(username)
        domain.validate_password_policy(
            password,
            min_length=self._auth.password.min_length,
            max_length=self._auth.password.max_length,
        )
        async with self._uow.begin() as session:
            existing = (
                await session.execute(select(UserRow).where(UserRow.username == normalized))
            ).scalar_one_or_none()
            if existing is not None:
                if not existing.is_platform_admin:
                    existing.is_platform_admin = True
                    existing.auth_revision += 1
                return self._profile(existing)
            user = UserRow(
                username=normalized,
                display_name=(display_name or normalized)[:128],
                password_hash=domain.hash_password(password),
                status="active",
                is_platform_admin=True,
                auth_revision=1,
            )
            session.add(user)
            await session.flush()
            return self._profile(user)

    async def create_user(
        self,
        *,
        username: str,
        password: str,
        display_name: str | None,
        is_platform_admin: bool = False,
    ) -> UserProfile:
        normalized = domain.normalize_username(username)
        domain.validate_password_policy(
            password,
            min_length=self._auth.password.min_length,
            max_length=self._auth.password.max_length,
        )
        async with self._uow.begin() as session:
            user = UserRow(
                username=normalized,
                display_name=(display_name or normalized)[:128],
                password_hash=domain.hash_password(password),
                status="active",
                is_platform_admin=is_platform_admin,
                auth_revision=1,
            )
            session.add(user)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise AlreadyExistsError("用户名已存在") from exc
            return self._profile(user)

    async def set_user_status(self, user_id: uuid.UUID, status: str) -> UserProfile:
        if status not in ("active", "disabled"):
            raise InvalidInputError("status 只允许 active/disabled")
        async with self._uow.begin() as session:
            user = (await session.execute(select(UserRow).where(UserRow.id == user_id))).scalar_one_or_none()
            if user is None:
                raise NotFoundError("用户不存在")
            user.status = status
            user.auth_revision += 1
            if status == "disabled":
                now = self._clock.now()
                await session.execute(
                    update(UserSessionRow)
                    .where(UserSessionRow.user_id == user_id, UserSessionRow.revoked_at.is_(None))
                    .values(revoked_at=now)
                )
            return self._profile(user)

    async def get_user(self, user_id: uuid.UUID) -> UserProfile:
        async with self._uow.begin() as session:
            user = (await session.execute(select(UserRow).where(UserRow.id == user_id))).scalar_one_or_none()
            if user is None:
                raise NotFoundError("用户不存在")
            return self._profile(user)

    async def create_tenant(self, name: str) -> uuid.UUID:
        clean = name.strip()
        if not 1 <= len(clean) <= 128:
            raise ValidationFailedError("租户名称长度需在 1-128 之间")
        async with self._uow.begin() as session:
            tenant = TenantRow(name=clean, status="active")
            session.add(tenant)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise AlreadyExistsError("租户名称已存在") from exc
            return tenant.id

    async def set_tenant_status(self, tenant_id: uuid.UUID, status: str) -> None:
        if status not in ("active", "disabled"):
            raise InvalidInputError("status 只允许 active/disabled")
        async with self._uow.begin() as session:
            tenant = (
                await session.execute(select(TenantRow).where(TenantRow.id == tenant_id))
            ).scalar_one_or_none()
            if tenant is None:
                raise NotFoundError("租户不存在")
            tenant.status = status

    async def list_tenants_for_user(self, user_id: uuid.UUID) -> list[TenantInfo]:
        """仅返回本人有效 memberships（含租户当前状态，便于前端禁用切换）。"""
        async with self._uow.begin() as session:
            rows = (
                await session.execute(
                    select(MembershipRow, TenantRow)
                    .join(TenantRow, TenantRow.id == MembershipRow.tenant_id)
                    .where(
                        MembershipRow.user_id == user_id,
                        MembershipRow.status == "active",
                    )
                    .order_by(TenantRow.name)
                )
            ).all()
            return [
                TenantInfo(
                    tenant_id=tenant.id,
                    name=tenant.name,
                    tenant_status=tenant.status,
                    role=membership.role,
                    membership_id=membership.id,
                    membership_status=membership.status,
                )
                for membership, tenant in rows
            ]

    async def resolve_tenant_membership(self, user_id: uuid.UUID, tenant_id: uuid.UUID) -> TenantInfo:
        """校验 X-Tenant-ID 对应本人有效 membership 且租户有效。"""
        async with self._uow.begin() as session:
            row = (
                await session.execute(
                    select(MembershipRow, TenantRow)
                    .join(TenantRow, TenantRow.id == MembershipRow.tenant_id)
                    .where(
                        MembershipRow.user_id == user_id,
                        MembershipRow.tenant_id == tenant_id,
                    )
                )
            ).one_or_none()
            if row is None:
                # 不区分不存在 / 非成员，统一 403，避免枚举
                raise ForbiddenError("不是该租户的有效成员")
            membership, tenant = row
            if membership.status != "active":
                raise ForbiddenError("成员关系已停用")
            if tenant.status != "active":
                raise ConflictError("租户已停用")
            return TenantInfo(
                tenant_id=tenant.id,
                name=tenant.name,
                tenant_status=tenant.status,
                role=membership.role,
                membership_id=membership.id,
                membership_status=membership.status,
            )

    # ------------------------------------------------------------------
    # 成员管理（租户 admin）
    # ------------------------------------------------------------------

    async def add_member(self, tenant_id: uuid.UUID, username: str, role: str) -> TenantInfo:
        if role not in domain.TenantRole.ALL:
            raise InvalidInputError("角色只允许 admin/editor/viewer")
        normalized = domain.normalize_username(username)
        async with self._uow.begin() as session:
            user = (
                await session.execute(select(UserRow).where(UserRow.username == normalized))
            ).scalar_one_or_none()
            if user is None:
                raise NotFoundError("用户不存在")
            membership = MembershipRow(tenant_id=tenant_id, user_id=user.id, role=role, status="active")
            session.add(membership)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise AlreadyExistsError("该用户已是本租户成员") from exc
            return TenantInfo(
                tenant_id=tenant_id,
                name="",
                tenant_status="active",
                role=role,
                membership_id=membership.id,
                membership_status="active",
            )

    async def update_member_role(self, tenant_id: uuid.UUID, membership_id: uuid.UUID, role: str) -> None:
        if role not in domain.TenantRole.ALL:
            raise InvalidInputError("角色只允许 admin/editor/viewer")
        async with self._uow.begin() as session:
            membership = await self._get_membership(session, tenant_id, membership_id)
            if membership.role == "admin" and role != "admin":
                await self._ensure_not_last_admin(session, tenant_id, membership_id)
            membership.role = role
            membership.auth_revision += 1

    async def set_member_status(self, tenant_id: uuid.UUID, membership_id: uuid.UUID, status: str) -> None:
        if status not in ("active", "disabled"):
            raise InvalidInputError("status 只允许 active/disabled")
        async with self._uow.begin() as session:
            membership = await self._get_membership(session, tenant_id, membership_id)
            if membership.role == "admin" and status != "active":
                await self._ensure_not_last_admin(session, tenant_id, membership_id)
            membership.status = status
            membership.auth_revision += 1

    async def remove_member(self, tenant_id: uuid.UUID, membership_id: uuid.UUID) -> None:
        async with self._uow.begin() as session:
            membership = await self._get_membership(session, tenant_id, membership_id)
            if membership.role == "admin":
                await self._ensure_not_last_admin(session, tenant_id, membership_id)
            await session.execute(delete(MembershipRow).where(MembershipRow.id == membership_id))

    async def list_members(self, tenant_id: uuid.UUID) -> list[dict[str, object]]:
        async with self._uow.begin() as session:
            rows = (
                await session.execute(
                    select(MembershipRow, UserRow)
                    .join(UserRow, UserRow.id == MembershipRow.user_id)
                    .where(MembershipRow.tenant_id == tenant_id)
                    .order_by(MembershipRow.created_at)
                )
            ).all()
            return [
                {
                    "membership_id": str(m.id),
                    "user_id": str(u.id),
                    "username": u.username,
                    "display_name": u.display_name,
                    "role": m.role,
                    "status": m.status,
                    "user_status": u.status,
                }
                for m, u in rows
            ]

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _enforce_login_rate_limit(self, username: str) -> None:
        window_start = self._clock.now() - timedelta(seconds=self._auth.login_rate_limit.window_seconds)
        async with self._uow.read_only() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(AuditLogRow)
                    .where(
                        AuditLogRow.action == "auth.login_failed",
                        AuditLogRow.resource_type == "user",
                        AuditLogRow.resource_id == username,
                        AuditLogRow.created_at >= window_start,
                    )
                )
            ).scalar_one()
        if count >= self._auth.login_rate_limit.max_attempts:
            raise RateLimitedError("登录尝试过于频繁，请稍后再试")

    async def _get_membership(
        self, session: object, tenant_id: uuid.UUID, membership_id: uuid.UUID
    ) -> MembershipRow:
        membership = (
            await session.execute(  # type: ignore[attr-defined]
                select(MembershipRow).where(
                    MembershipRow.id == membership_id,
                    MembershipRow.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            raise NotFoundError("成员不存在")
        return cast(MembershipRow, membership)

    async def _ensure_not_last_admin(
        self, session: object, tenant_id: uuid.UUID, membership_id: uuid.UUID
    ) -> None:
        others = (
            await session.execute(  # type: ignore[attr-defined]
                select(func.count())
                .select_from(MembershipRow)
                .where(
                    MembershipRow.tenant_id == tenant_id,
                    MembershipRow.role == "admin",
                    MembershipRow.status == "active",
                    MembershipRow.id != membership_id,
                )
            )
        ).scalar_one()
        if others == 0:
            raise ConflictError("不能移除或停用租户的最后一个有效 admin")

    @staticmethod
    def _profile(user: UserRow) -> UserProfile:
        return UserProfile(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            is_platform_admin=user.is_platform_admin,
            status=user.status,
            auth_revision=user.auth_revision,
        )

    @staticmethod
    def tenant_context(tenant_id: uuid.UUID) -> TenantContext:
        return TenantContext(tenant_id=tenant_id)
