"""哈希与凭据摘要工具。

- 凭据（Session / 应用 Token）只存 HMAC-SHA-256 摘要与 pepper key version。
- 校验使用常量时间比较。
- 用户密码哈希使用 Argon2id（见 passlib_argon2 封装）。
"""

from __future__ import annotations

import hashlib
import hmac


def hmac_sha256_digest(secret: str, pepper: str) -> str:
    """对凭据计算 HMAC-SHA-256 摘要（hex）。pepper 来自 SecretResolver。"""
    return hmac.new(pepper.encode("utf-8"), secret.encode("utf-8"), hashlib.sha256).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class StreamingSha256:
    """流式计算 SHA-256，用于上传/下载的服务端校验。"""

    def __init__(self) -> None:
        self._hasher = hashlib.sha256()
        self._bytes = 0

    def update(self, chunk: bytes) -> None:
        self._hasher.update(chunk)
        self._bytes += len(chunk)

    @property
    def byte_count(self) -> int:
        return self._bytes

    def hexdigest(self) -> str:
        return self._hasher.hexdigest()
