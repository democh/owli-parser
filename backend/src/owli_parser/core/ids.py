"""ID 与安全随机串生成。

- 业务主键统一 UUID4（job_id 为独立 UUID，不与请求幂等键复用）。
- 凭据/票据类使用 CSPRNG 高熵随机串。
"""

from __future__ import annotations

import secrets
import uuid
from typing import Protocol

_SECRET_MIN_BYTES = 32  # 256 bit 安全下限（协议不变量）


class IdGenerator(Protocol):
    def new_id(self) -> uuid.UUID: ...


class UuidGenerator:
    def new_id(self) -> uuid.UUID:
        return uuid.uuid4()


def new_secret(nbytes: int = _SECRET_MIN_BYTES) -> str:
    """生成高熵随机串（hex，无下划线等歧义字符）。nbytes 不得低于安全下限。"""
    if nbytes < _SECRET_MIN_BYTES:
        raise ValueError(f"secret 熵不得低于 {_SECRET_MIN_BYTES} 字节")
    return secrets.token_hex(nbytes)


def new_public_id(nbytes: int = 12) -> str:
    """可对外展示的公开标识（hex，非秘密），如 Token public_id。"""
    return secrets.token_hex(nbytes)


def uuid_str() -> str:
    return str(uuid.uuid4())


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return True
