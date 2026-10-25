"""受控来源 URL 的加密存储。

- 预签名 URL 的 query 可能等价于密码：完整 URL 加密保存，仅业务进程可解。
- 密钥来自 SecretResolver（env:OWLI_SOURCE_URL_KEY，base64 32 字节）。
- 存放到受控目录，导入结束按保留策略回收。
"""

from __future__ import annotations

import base64
import contextlib
import os
import uuid
from pathlib import Path
from typing import Protocol, cast

from owli_parser.ports.secrets import SecretResolver, SecretUnavailable


class SourceSecretStore(Protocol):
    def store(self, tenant_id: uuid.UUID, import_id: uuid.UUID, value: str) -> str: ...

    def load(self, ref: str) -> str: ...

    def delete(self, ref: str) -> None: ...


class EncryptedSourceStore:
    """Fernet 加密文件存储；ref 形如 `enc:{tenant}/{import_id}`。"""

    def __init__(self, root: Path, key_ref: str, secrets: SecretResolver) -> None:
        self._root = root
        self._key_ref = key_ref
        self._secrets = secrets
        self._fernet: object | None = None

    def _cipher(self) -> object:
        if self._fernet is None:
            try:
                from cryptography.fernet import Fernet
            except ImportError as exc:  # pragma: no cover
                raise SecretUnavailable("cryptography 未安装") from exc
            try:
                raw = self._secrets.resolve(self._key_ref)
            except SecretUnavailable as exc:
                raise SecretUnavailable("来源 URL 加密密钥缺失（配置 imports.url_secret_key_ref）") from exc
            # 接受 base64 Fernet key 或原文 32 字节
            try:
                key = raw.encode("ascii")
                base64.urlsafe_b64decode(key)
                fernet_key = key
            except Exception:
                import hashlib

                fernet_key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
            self._fernet = Fernet(fernet_key)
        return self._fernet

    def store(self, tenant_id: uuid.UUID, import_id: uuid.UUID, value: str) -> str:
        ref = f"enc:{tenant_id}/{import_id}"
        path = self._path_for(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        token = self._cipher().encrypt(value.encode("utf-8"))  # type: ignore[attr-defined]
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(token)
        return ref

    def load(self, ref: str) -> str:
        path = self._path_for(ref)
        try:
            token = path.read_bytes()
        except OSError as exc:
            raise SecretUnavailable("来源引用已过期或不存在") from exc
        try:
            return cast(str, self._cipher().decrypt(token).decode("utf-8"))  # type: ignore[attr-defined]
        except Exception as exc:
            raise SecretUnavailable("来源引用解密失败") from exc

    def delete(self, ref: str) -> None:
        with contextlib.suppress(OSError):  # 清理失败不阻塞主流程
            self._path_for(ref).unlink(missing_ok=True)

    def _path_for(self, ref: str) -> Path:
        if not ref.startswith("enc:"):
            raise SecretUnavailable("不支持的来源引用协议")
        _, rest = ref.split(":", 1)
        parts = rest.split("/")
        if len(parts) != 2 or any(p in ("..", "", ".") for p in parts):
            raise SecretUnavailable("来源引用不合法")
        return self._root / parts[0] / f"{parts[1]}.enc"
