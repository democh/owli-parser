"""jobs 领域逻辑：状态转换校验与重试退避。"""

from __future__ import annotations

import random

from owli_parser.contracts.events import ALLOWED_JOB_TRANSITIONS, TERMINAL_JOB_STATES, JobState
from owli_parser.core.errors import InvalidStateError


def assert_transition(from_state: str, to_state: str) -> None:
    current = JobState(from_state)
    target = JobState(to_state)
    allowed = ALLOWED_JOB_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidStateError(f"不允许的任务状态转换: {from_state} -> {to_state}")


def is_terminal(state: str) -> bool:
    return JobState(state) in TERMINAL_JOB_STATES


def compute_backoff_seconds(
    attempt: int, *, base_seconds: int, max_seconds: int, jitter_ratio: float = 0.2
) -> float:
    """指数退避 + jitter；尊重总预算由调用方控制。"""
    raw = float(min(base_seconds * (2 ** max(attempt - 1, 0)), max_seconds))
    jitter = raw * jitter_ratio * (random.random() * 2 - 1)  # noqa: S311 - 退避抖动无需密码学随机
    return max(1.0, raw + jitter)


def classify_failure(error_code: str) -> str:
    """错误分类：retryable / fatal / policy。

    - 网络/限流类可重试；参数、加密、损坏输入、权限/政策错误不重试。
    """
    retryable_codes = {
        "upstream_timeout",
        "external_service_unavailable",
        "fetch_failed",
        "backpressure",
        "rate_limited",
        "storage_unavailable",
    }
    policy_codes = {
        "network_policy_denied",
        "ssrf_blocked",
        "forbidden",
        "scope_required",
        "authorization_revoked",
    }
    if error_code in retryable_codes:
        return "retryable"
    if error_code in policy_codes:
        return "policy"
    return "fatal"
