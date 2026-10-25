"""SafeFetcher 实现：受控 URL 流式下载（SSRF 防护核心）。

防护要点（文档第五部分 §5.2）：
1. 拒绝 userinfo、控制字符、非白名单 scheme、非法端口与过长输入。
2. 每一跳 DNS 解析检查所有候选 IPv4/IPv6：阻止 loopback、link-local、
   元数据、私网（未获准）、组播、未指定/保留地址与 IPv4-mapped IPv6 绕过。
3. 连接后核对实际 peer 地址是否在已验证集合内（防 DNS rebinding）。
4. 重定向逐跳验证；跨 origin 不转发凭据；HTTPS 降级拒绝；禁用隐式环境代理。
5. GET 流式下载；实际字节数、超时与停滞检测；超限即中止。
"""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable
from typing import BinaryIO
from urllib.parse import urlparse

import httpx

from owli_parser.core.crypto import StreamingSha256
from owli_parser.ports.fetcher import (
    FetchBlocked,
    FetchError,
    FetchPolicy,
    FetchResult,
)

_CHUNK_SIZE = 64 * 1024
_MAX_URL_LENGTH = 2048
_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)


def _parse_and_validate_url(url: str, policy: FetchPolicy) -> tuple[str, str, int]:
    """返回 (scheme, host, port)。任何不安全结构直接拒绝。"""
    if len(url) > _MAX_URL_LENGTH:
        raise FetchBlocked("URL 过长")
    if any(ord(c) < 0x20 for c in url):
        raise FetchBlocked("URL 含控制字符")
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise FetchBlocked("URL 无法解析") from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in tuple(policy.allowed_schemes):
        raise FetchBlocked(f"不允许的 URL scheme: {scheme or '(空)'}")
    if parsed.username or parsed.password:
        raise FetchBlocked("URL 不允许包含 userinfo")
    host = parsed.hostname
    if not host:
        raise FetchBlocked("URL 缺少主机名")
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise FetchBlocked("URL 端口非法") from exc
    if port not in tuple(policy.allowed_ports):
        raise FetchBlocked(f"不允许的端口: {port}")
    return scheme, host.lower(), port


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """返回禁止原因；None 表示该地址本身类别合法（私网另行判定）。"""
    # IPv4-mapped IPv6 / 6to4 / Teredo 等解包
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _is_forbidden_ip(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return _is_forbidden_ip(ip.sixtofour)
        if getattr(ip, "teredo", None) is not None:
            return "teredo 地址不允许"
    if ip.is_loopback:
        return "loopback 地址不允许"
    if ip.is_link_local:
        return "link-local 地址不允许"
    if ip.is_multicast:
        return "组播地址不允许"
    if ip.is_unspecified:
        return "未指定地址不允许"
    if ip.is_reserved:
        return "保留地址不允许"
    if str(ip) == "169.254.169.254":
        return "元数据服务地址不允许"
    return None


def _resolve_and_check(host: str, port: int, policy: FetchPolicy) -> list[str]:
    """解析全部候选地址并逐一校验；任何地址非法即整体拒绝（保守策略）。"""
    host_in_allowlist = f"{host}:{port}" in tuple(policy.internal_allowlist)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchError("DNS 解析失败", retryable=True) from exc
    addresses: list[str] = []
    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise FetchBlocked("DNS 返回非法地址") from exc
        reason = _is_forbidden_ip(ip)
        if reason:
            raise FetchBlocked(f"{reason}: {host}")
        if ip.is_private and not (policy.allow_private_networks or host_in_allowlist):
            raise FetchBlocked(f"私网地址不允许: {host}")
        addresses.append(str(ip))
    if not addresses:
        raise FetchError("DNS 未返回可用地址", retryable=True)
    return addresses


def _is_retryable_status(status_code: int, policy: FetchPolicy) -> bool:
    return status_code in policy.retryable_status_codes


def _check_network_mode(host: str, port: int, policy: FetchPolicy) -> None:
    """offline 模式禁止公网 URL，但允许明确批准的内网服务。"""
    if policy.network_mode == "offline" and f"{host}:{port}" not in tuple(policy.internal_allowlist):
        raise FetchBlocked("offline 模式禁止访问该地址（需明确批准的内网服务）")


class HttpxSafeFetcher:
    """基于 httpx 的 SafeFetcher 同步实现（用于 worker 进程）。

    - 默认禁用环境代理；显式代理也必须落实目标地址策略（此处直接拒绝代理组合）。
    - 手动处理重定向以逐跳校验，不依赖 HTTP 库的自动跳转。
    """

    def fetch(
        self,
        url: str,
        *,
        policy: FetchPolicy,
        dest_factory: Callable[[], BinaryIO],
        on_progress: Callable[[int, int | None], None] | None = None,
        cancellation: Callable[[], bool] | None = None,
    ) -> FetchResult:
        deadline = time.monotonic() + policy.total_timeout_seconds
        current_url = url
        redirect_count = 0
        last_host = ""
        while True:
            if cancellation is not None and cancellation():
                raise FetchError("下载被取消", retryable=False)
            scheme, host, port = _parse_and_validate_url(current_url, policy)
            _check_network_mode(host, port, policy)
            if host in _METADATA_HOSTS:
                raise FetchBlocked("元数据服务主机不允许")
            verified_ips = _resolve_and_check(host, port, policy)
            last_host = host

            remaining_budget = deadline - time.monotonic()
            if remaining_budget <= 0:
                raise FetchError("下载总超时", retryable=True)
            timeout = httpx.Timeout(
                connect=min(policy.connect_timeout_seconds, remaining_budget),
                read=min(policy.read_timeout_seconds, remaining_budget),
                write=min(policy.read_timeout_seconds, remaining_budget),
                pool=min(policy.connect_timeout_seconds, remaining_budget),
            )
            try:
                with (
                    httpx.Client(
                        follow_redirects=False,
                        timeout=timeout,
                        proxy=None,  # 显式不使用环境代理
                        verify=policy.tls_ca_bundle or True,
                        trust_env=False,
                    ) as client,
                    client.stream("GET", current_url) as response,
                ):
                    self._verify_peer(response, verified_ips)
                    if response.status_code in (301, 302, 303, 307, 308):
                        redirect_count += 1
                        if redirect_count > policy.max_redirects:
                            raise FetchError("重定向次数超出上限", retryable=False)
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError("重定向缺少 Location", retryable=False)
                        next_url = str(httpx.URL(current_url).join(location))
                        next_scheme, next_host, next_port = _parse_and_validate_url(next_url, policy)
                        if scheme == "https" and next_scheme == "http":
                            raise FetchBlocked("拒绝 HTTPS 降级重定向")
                        del next_host, next_port
                        current_url = next_url
                        continue

                    if response.status_code >= 400:
                        retryable = _is_retryable_status(response.status_code, policy)
                        raise FetchError(
                            f"HTTP {response.status_code}",
                            retryable=retryable,
                            status_code=response.status_code,
                        )

                    content_type = response.headers.get("content-type")
                    declared_length = response.headers.get("content-length")
                    total_hint = (
                        int(declared_length) if declared_length and declared_length.isdigit() else None
                    )
                    if total_hint is not None and total_hint > policy.max_bytes:
                        raise FetchError("响应声明大小超出上限", retryable=False)

                    hasher = StreamingSha256()
                    stalled_deadline = time.monotonic() + policy.stalled_timeout_seconds
                    with dest_factory() as dest:
                        for chunk in response.iter_bytes(_CHUNK_SIZE):
                            if cancellation is not None and cancellation():
                                raise FetchError("下载被取消", retryable=False)
                            now = time.monotonic()
                            if now > deadline:
                                raise FetchError("下载总超时", retryable=True)
                            if chunk:
                                stalled_deadline = now + policy.stalled_timeout_seconds
                            hasher.update(chunk)
                            if hasher.byte_count > policy.max_bytes:
                                raise FetchError("实际字节数超出上限", retryable=False)
                            dest.write(chunk)
                            if on_progress is not None:
                                on_progress(hasher.byte_count, total_hint)
                        if time.monotonic() > stalled_deadline:
                            raise FetchError("下载停滞超时", retryable=True)
                    return FetchResult(
                        size_bytes=hasher.byte_count,
                        sha256=hasher.hexdigest(),
                        content_type_hint=content_type,
                        final_host=last_host,
                        redirect_count=redirect_count,
                        temp_path=getattr(dest, "name", ""),
                    )
            except httpx.TimeoutException as exc:
                raise FetchError("网络超时", retryable=True) from exc
            except httpx.TransportError as exc:
                raise FetchError("网络传输失败", retryable=True) from exc

    @staticmethod
    def _verify_peer(response: httpx.Response, verified_ips: list[str]) -> None:
        """核对实际连接的对端地址在已验证集合内（防 DNS rebinding）。"""
        stream = response.extensions.get("network_stream")
        if stream is None:
            raise FetchBlocked("无法核对连接对端地址")
        try:
            server_addr = stream.get_extra_info("server_addr")
        except Exception as exc:
            raise FetchBlocked("无法核对连接对端地址") from exc
        if not server_addr:
            raise FetchBlocked("无法核对连接对端地址")
        peer_ip = str(server_addr[0])
        if peer_ip not in verified_ips:
            raise FetchBlocked("连接对端与已验证地址不一致（疑似 DNS rebinding）")
