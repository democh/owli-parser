"""Secret 端口：受控密钥解析。

- 密钥只存引用（env:/file:），权限独立；配置快照只记录 ref/version。
- 明文不进入日志、前端响应与数据库快照。
"""

from __future__ import annotations

from typing import Protocol


class SecretUnavailable(Exception):
    """密钥引用不可解析（缺失/权限不足）。fail closed，不静默降级。"""


class SecretResolver(Protocol):
    def resolve(self, ref: str) -> str:
        """解析 secret 引用为明文。仅限进程内使用，禁止透出到响应/日志。"""
        ...

    def exists(self, ref: str) -> bool: ...


class EnvSecretResolver:
    """支持 env:NAME 与 file:/path（文件权限由部署侧控制）引用。"""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        import os

        self._env = env if env is not None else dict(os.environ)

    def resolve(self, ref: str) -> str:
        if ref.startswith("env:"):
            name = ref[4:]
            value = self._env.get(name)
            if value is None or value == "":
                raise SecretUnavailable(f"环境变量 {name} 未设置")
            return value
        if ref.startswith("file:"):
            from pathlib import Path

            path = Path(ref[5:])
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise SecretUnavailable(f"secret 文件不可读: {path.name}") from exc
            if not value:
                raise SecretUnavailable("secret 文件为空")
            return value
        raise SecretUnavailable("不支持的 secret 引用协议（支持 env:/file:）")

    def exists(self, ref: str) -> bool:
        try:
            self.resolve(ref)
        except SecretUnavailable:
            return False
        return True


class StaticSecretResolver:
    """测试替身：字典映射。生产不使用。"""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = values or {}

    def resolve(self, ref: str) -> str:
        try:
            return self._values[ref]
        except KeyError as exc:
            raise SecretUnavailable(f"测试 secret 未提供: {ref}") from exc

    def exists(self, ref: str) -> bool:
        return ref in self._values
