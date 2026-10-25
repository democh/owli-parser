"""access 领域逻辑：Token 格式与解析、授权判定纯函数。

Token 格式：`{prefix}_{public_id}_{secret}`
- prefix 来自集中配置（默认 owli_pat），作为协议前缀集中定义。
- public_id 为可展示标识（非秘密）；secret 为高熵随机串（≥256 bit）。
- 数据库存 HMAC-SHA-256(secret, pepper) 摘要，不存明文。
"""

from __future__ import annotations

from dataclasses import dataclass

from owli_parser.core.crypto import hmac_sha256_digest
from owli_parser.core.errors import InvalidInputError, NotAuthenticatedError
from owli_parser.core.ids import new_public_id, new_secret


@dataclass(frozen=True, slots=True)
class ParsedToken:
    public_id: str
    secret: str


def generate_token(prefix: str) -> tuple[str, ParsedToken]:
    """返回 (完整明文 token, 解析结构)。明文只在签发响应中出现一次。"""
    public_id = new_public_id(12)
    secret = new_secret(32)
    token = f"{prefix}_{public_id}_{secret}"
    return token, ParsedToken(public_id=public_id, secret=secret)


def parse_token(prefix: str, raw: str) -> ParsedToken:
    """严格解析；格式错误统一返回未认证，不泄露解析细节。"""
    if not raw or len(raw) > 256:
        raise NotAuthenticatedError("Token 格式不合法")
    # 前缀固定为配置值，直接以 (prefix + "_") 校验（prefix 本身可含下划线）
    expected_prefix = f"{prefix}_"
    if not raw.startswith(expected_prefix):
        raise NotAuthenticatedError("Token 格式不合法")
    remainder = raw[len(expected_prefix) :]
    if remainder.count("_") != 1:
        raise NotAuthenticatedError("Token 格式不合法")
    public_id, secret = remainder.split("_", 1)
    if not public_id or not secret:
        raise NotAuthenticatedError("Token 格式不合法")
    return ParsedToken(public_id=public_id, secret=secret)


def compute_digest(raw: str, pepper: str) -> str:
    """Token secret 的 HMAC-SHA-256 摘要（hex）。数据库只存摘要，不存明文。"""
    return hmac_sha256_digest(raw, pepper)


def validate_scopes_subset(token_scopes: list[str], application_scopes: list[str]) -> None:
    """token.scopes 必须是 application.allowed_scopes 的子集。"""
    allowed = set(application_scopes)
    invalid = [s for s in token_scopes if s not in allowed]
    if invalid:
        raise InvalidInputError(f"Token scope 超出应用允许范围: {', '.join(invalid)}")


def validate_dataset_ids_subset(token_dataset_ids: list[str], granted_dataset_ids: set[str]) -> None:
    invalid = [d for d in token_dataset_ids if d not in granted_dataset_ids]
    if invalid:
        raise InvalidInputError("Token 数据集不在应用授权范围内")


def validate_ttl(expires_in_seconds: int, *, default_ttl: int, max_ttl: int) -> int:
    """必填到期时间；不签发永不过期 Token。"""
    if expires_in_seconds <= 0:
        raise InvalidInputError("Token 必须设置正的到期时间")
    if expires_in_seconds > max_ttl:
        raise InvalidInputError(f"Token TTL 不得超过 {max_ttl} 秒")
    return min(expires_in_seconds, max_ttl)
