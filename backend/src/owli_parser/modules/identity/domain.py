"""identity 领域逻辑：密码策略与 Argon2id 哈希、Session/CSRF 凭据生成。

- 用户密码使用 Argon2id；Session 为服务端不透明令牌，数据库只存 HMAC 摘要。
- 登录失败计数用于限流；用户名规范化后唯一。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from owli_parser.core.crypto import hmac_sha256_digest
from owli_parser.core.errors import InvalidInputError, ValidationFailedError
from owli_parser.core.ids import new_secret

_USERNAME_RE = re.compile(r"^[a-z0-9._@+-]{3,64}$")

# 工作因子为初始配置；OWASP 推荐量级，进程启动时构建一次。
_ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)

DIGEST_KEY_VERSION = 1


class TenantRole:
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"

    ALL = frozenset({ADMIN, EDITOR, VIEWER})


def normalize_username(raw: str) -> str:
    username = raw.strip().lower()
    if not _USERNAME_RE.match(username):
        raise InvalidInputError("用户名只允许 3-64 位小写字母、数字及 . _ @ + -（规范化后）")
    return username


def validate_password_policy(password: str, *, min_length: int, max_length: int) -> None:
    if len(password) < min_length:
        raise ValidationFailedError(f"密码长度不得少于 {min_length} 个字符")
    if len(password) > max_length:
        raise ValidationFailedError(f"密码长度不得超过 {max_length} 个字符")
    if password.strip() == "":
        raise ValidationFailedError("密码不得为空白")


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _ph.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(stored_hash: str) -> bool:
    try:
        return _ph.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """登录后一次性返回的凭据（不落库明文）。"""

    session_id: str  # UUID 字符串
    session_token: str
    csrf_token: str


def issue_session_credentials() -> tuple[str, str]:
    """生成 (session_token, csrf_token)；两者均为高熵随机串。"""
    return new_secret(32), new_secret(32)


def new_csrf_token() -> str:
    """生成新的 CSRF 明文令牌（只存摘要）。"""
    return new_secret(32)


def compute_digest(raw: str, pepper: str) -> str:
    return hmac_sha256_digest(raw, pepper)
