"""稳定错误码与业务异常体系。

- 业务错误使用稳定 ErrorCode，禁止吞异常后返回空成功结果。
- HTTP 映射见文档后端 §12：400/401/403/404/409/412/413/429/503。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    # 输入与请求
    INVALID_INPUT = "invalid_input"
    VALIDATION_FAILED = "validation_failed"
    MALFORMED_REQUEST = "malformed_request"
    # 认证与授权
    NOT_AUTHENTICATED = "not_authenticated"
    INVALID_CREDENTIALS = "invalid_credentials"
    SESSION_EXPIRED = "session_expired"
    FORBIDDEN = "forbidden"
    SCOPE_REQUIRED = "scope_required"
    TENANT_MISMATCH = "tenant_mismatch"
    TENANT_INACTIVE = "tenant_inactive"
    MEMBERSHIP_REQUIRED = "membership_required"
    CSRF_FAILED = "csrf_failed"
    # 资源
    NOT_FOUND = "not_found"
    ALREADY_EXISTS = "already_exists"
    REVISION_CONFLICT = "revision_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    RESOURCE_DELETED = "resource_deleted"
    # 容量与配额
    PAYLOAD_TOO_LARGE = "payload_too_large"
    QUOTA_EXCEEDED = "quota_exceeded"
    RATE_LIMITED = "rate_limited"
    BACKPRESSURE = "backpressure"
    # 状态与流程
    INVALID_STATE = "invalid_state"
    CONFLICT = "conflict"
    UNSUPPORTED_FORMAT = "unsupported_format"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    CAPABILITY_NOT_READY = "capability_not_ready"
    AUTHORIZATION_REVOKED = "authorization_revoked"
    # 网络与外部
    NETWORK_POLICY_DENIED = "network_policy_denied"
    FETCH_FAILED = "fetch_failed"
    EXTERNAL_SERVICE_UNAVAILABLE = "external_service_unavailable"
    SSRF_BLOCKED = "ssrf_blocked"
    # 存储
    STORAGE_NOT_FOUND = "storage_not_found"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    # 系统
    INTERNAL_ERROR = "internal_error"
    CONFIG_INVALID = "config_invalid"
    UPSTREAM_TIMEOUT = "upstream_timeout"


class AppError(Exception):
    """业务异常基类。

    code 为稳定协议值；message 面向调用者；
    detail 只放结构化字段错误，不得携带密钥、JWT、预签名 URL 或原始 SDK 异常。
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        http_status: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.detail = detail or {}

    def to_body(self, trace_id: str) -> dict[str, Any]:
        return {
            "code": str(self.code),
            "message": self.message,
            "detail": self.detail,
            "trace_id": trace_id,
        }


def _make(code: ErrorCode, http_status: int, default_message: str) -> type[AppError]:
    class _Error(AppError):
        def __init__(self, message: str = default_message, **kwargs: Any) -> None:
            kwargs.setdefault("code", code)
            kwargs.setdefault("http_status", http_status)
            super().__init__(message, **kwargs)

    _Error.__name__ = code.value.title().replace("_", "")
    return _Error


InvalidInputError = _make(ErrorCode.INVALID_INPUT, 400, "请求输入不合法")
ValidationFailedError = _make(ErrorCode.VALIDATION_FAILED, 422, "输入校验失败")
NotAuthenticatedError = _make(ErrorCode.NOT_AUTHENTICATED, 401, "未认证或凭据无效")
SessionExpiredError = _make(ErrorCode.SESSION_EXPIRED, 401, "会话已过期")
ForbiddenError = _make(ErrorCode.FORBIDDEN, 403, "无权执行此操作")
ScopeRequiredError = _make(ErrorCode.SCOPE_REQUIRED, 403, "应用 Token 缺少所需 scope")
TenantMismatchError = _make(ErrorCode.TENANT_MISMATCH, 403, "租户上下文不一致")
MembershipRequiredError = _make(ErrorCode.MEMBERSHIP_REQUIRED, 403, "不是该租户的有效成员")
NotFoundError = _make(ErrorCode.NOT_FOUND, 404, "资源不存在或不可用")
AlreadyExistsError = _make(ErrorCode.ALREADY_EXISTS, 409, "资源已存在")
RevisionConflictError = _make(ErrorCode.REVISION_CONFLICT, 412, "资源已被其他操作修改")
IdempotencyConflictError = _make(ErrorCode.IDEMPOTENCY_CONFLICT, 409, "同一幂等键对应了不同请求")
ConflictError = _make(ErrorCode.CONFLICT, 409, "当前资源状态不允许此操作")
InvalidStateError = _make(ErrorCode.INVALID_STATE, 409, "资源状态不允许该转换")
PayloadTooLargeError = _make(ErrorCode.PAYLOAD_TOO_LARGE, 413, "请求体超出限制")
QuotaExceededError = _make(ErrorCode.QUOTA_EXCEEDED, 429, "超出配额限制")
RateLimitedError = _make(ErrorCode.RATE_LIMITED, 429, "请求频率超限")
UnsupportedFormatError = _make(ErrorCode.UNSUPPORTED_FORMAT, 422, "不支持的文件格式")
CapabilityNotReadyError = _make(ErrorCode.CAPABILITY_NOT_READY, 503, "所需能力尚未就绪")
NetworkPolicyDeniedError = _make(ErrorCode.NETWORK_POLICY_DENIED, 403, "网络策略拒绝该访问")
FetchFailedError = _make(ErrorCode.FETCH_FAILED, 502, "外部文件获取失败")
SsrfBlockedError = _make(ErrorCode.SSRF_BLOCKED, 403, "目标地址被安全策略阻止")
StorageNotFoundError = _make(ErrorCode.STORAGE_NOT_FOUND, 404, "存储对象不存在")
StorageUnavailableError = _make(ErrorCode.STORAGE_UNAVAILABLE, 503, "存储暂不可用")
ChecksumMismatchError = _make(ErrorCode.CHECKSUM_MISMATCH, 422, "内容校验失败")
ExternalServiceUnavailableError = _make(ErrorCode.EXTERNAL_SERVICE_UNAVAILABLE, 502, "外部服务不可用")
UpstreamTimeoutError = _make(ErrorCode.UPSTREAM_TIMEOUT, 504, "上游调用超时")
ConfigInvalidError = _make(ErrorCode.CONFIG_INVALID, 400, "配置不合法")
NotImplementedInStageError = _make(ErrorCode.CAPABILITY_NOT_READY, 503, "该能力将在后续里程碑提供")
