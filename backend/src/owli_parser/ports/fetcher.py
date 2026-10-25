"""SafeFetcher 端口：受控 URL 下载。

- 每一跳 DNS 解析检查所有候选地址；阻止 loopback/link-local/元数据/私网/组播等。
- 连接使用已验证的目的 IP 并核对实际 peer；逐跳验证重定向。
- GET 流式下载；实际字节数、MIME 与 magic 校验；超限即中止。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import BinaryIO, Protocol


@dataclasses.dataclass(frozen=True, slots=True)
class FetchPolicy:
    """按用途区分的抓取策略（imports 与模型端点互不共享 allowlist）。"""

    allowed_schemes: tuple[str, ...]
    allowed_ports: tuple[int, ...]
    max_redirects: int
    connect_timeout_seconds: float
    read_timeout_seconds: float
    total_timeout_seconds: float
    stalled_timeout_seconds: float
    max_bytes: int
    allow_private_networks: bool
    internal_allowlist: tuple[str, ...]
    # 仅这些上游状态码允许按下载预算重试；策略由集中配置下发。
    retryable_status_codes: tuple[int, ...] = (408, 425, 429, 500, 502, 503, 504)
    proxy_url: str | None = None
    tls_ca_bundle: str | None = None
    network_mode: str = "offline"


class FetchError(Exception):
    """下载失败。retryable 表示可按预算重试（网络/限流类）。"""

    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class FetchBlocked(Exception):
    """安全策略拒绝（SSRF、scheme/端口不允许、offline 禁止公网）。不重试。"""


@dataclasses.dataclass(frozen=True, slots=True)
class FetchResult:
    """下载结果：文件已落入隔离目录；content_type 为响应头提示，非可信凭据。"""

    size_bytes: int
    sha256: str
    content_type_hint: str | None
    final_host: str
    redirect_count: int
    temp_path: str


class SafeFetcher(Protocol):
    def fetch(
        self,
        url: str,
        *,
        policy: FetchPolicy,
        dest_factory: Callable[[], BinaryIO],
        on_progress: Callable[[int, int | None], None] | None = None,
        cancellation: Callable[[], bool] | None = None,
    ) -> FetchResult:
        """流式下载到 dest_factory() 打开的隔离文件对象。

        - 每跳 DNS 全量校验；重定向逐跳检查，跨 origin 不转发凭据。
        - 实际字节数超 max_bytes 中止；Content-Length 仅作提示。
        """
        ...
