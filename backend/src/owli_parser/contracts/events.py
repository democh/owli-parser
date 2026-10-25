"""通用事件 envelope 与 job/import 事件契约。

- 事件 envelope：schema_version、event_id、seq、type、tenant_scope、
  resource_id、attempt、ts、payload。
- seq 在单 job/chat run 内单调；持久化后再推送，客户端去重并忽略过期 attempt。
- 未知 major 明确拒绝，不静默误读。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EVENT_SCHEMA_VERSION = "1.0"
_EVENT_MAJOR = "1"


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = EVENT_SCHEMA_VERSION
    event_id: str
    seq: int = Field(ge=1)
    type: str
    tenant_scope: str
    resource_id: str
    attempt: int | None = Field(default=None, ge=1)
    ts: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def accepts_major(cls, version: str) -> bool:
        return version.split(".", 1)[0] == _EVENT_MAJOR


class JobState(StrEnum):
    PENDING = "PENDING"
    ROUTING = "ROUTING"
    PARSING = "PARSING"
    ENRICHING = "ENRICHING"
    NORMALIZING = "NORMALIZING"
    CHUNKING = "CHUNKING"
    INDEXING = "INDEXING"
    PUBLISHING = "PUBLISHING"
    DONE = "DONE"
    PARTIAL_DONE = "PARTIAL_DONE"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"


TERMINAL_JOB_STATES = frozenset(
    {
        JobState.DONE,
        JobState.PARTIAL_DONE,
        JobState.FAILED,
        JobState.DEAD_LETTER,
        JobState.CANCELLED,
    }
)

# 运行阶段顺序（DONE/PARTIAL_DONE 为发布后终态）
RUNNING_STAGES: tuple[JobState, ...] = (
    JobState.ROUTING,
    JobState.PARSING,
    JobState.ENRICHING,
    JobState.NORMALIZING,
    JobState.CHUNKING,
    JobState.INDEXING,
    JobState.PUBLISHING,
)

# PG 状态机允许的转换（CAS 提交前校验）
ALLOWED_JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.PENDING: frozenset({JobState.ROUTING, JobState.CANCELLING, JobState.FAILED}),
    JobState.ROUTING: frozenset(
        {
            JobState.PARSING,
            JobState.RETRY_WAIT,
            JobState.FAILED,
            JobState.DEAD_LETTER,
            JobState.CANCELLING,
        }
    ),
    JobState.PARSING: frozenset(
        {
            JobState.ENRICHING,
            JobState.NORMALIZING,
            JobState.RETRY_WAIT,
            JobState.FAILED,
            JobState.DEAD_LETTER,
            JobState.CANCELLING,
        }
    ),
    JobState.ENRICHING: frozenset(
        {
            JobState.NORMALIZING,
            JobState.RETRY_WAIT,
            JobState.FAILED,
            JobState.DEAD_LETTER,
            JobState.CANCELLING,
        }
    ),
    JobState.NORMALIZING: frozenset(
        {
            JobState.CHUNKING,
            JobState.RETRY_WAIT,
            JobState.FAILED,
            JobState.DEAD_LETTER,
            JobState.CANCELLING,
        }
    ),
    JobState.CHUNKING: frozenset(
        {
            JobState.INDEXING,
            JobState.RETRY_WAIT,
            JobState.FAILED,
            JobState.DEAD_LETTER,
            JobState.CANCELLING,
        }
    ),
    JobState.INDEXING: frozenset(
        {
            JobState.PUBLISHING,
            JobState.RETRY_WAIT,
            JobState.FAILED,
            JobState.DEAD_LETTER,
            JobState.CANCELLING,
        }
    ),
    JobState.PUBLISHING: frozenset(
        {JobState.DONE, JobState.PARTIAL_DONE, JobState.RETRY_WAIT, JobState.FAILED}
    ),
    JobState.RETRY_WAIT: frozenset(
        {JobState.ROUTING, JobState.PARSING, JobState.CANCELLING, JobState.DEAD_LETTER}
    ),
    JobState.CANCELLING: frozenset({JobState.CANCELLED}),
    JobState.DONE: frozenset(),
    JobState.PARTIAL_DONE: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.DEAD_LETTER: frozenset(),
    JobState.CANCELLED: frozenset(),
}

ImportState = Literal[
    "PENDING",
    "FETCHING",
    "VERIFYING",
    "READY",
    "SUBMITTED",
    "RETRY_WAIT",
    "CANCELLING",
    "CANCELLED",
    "FAILED",
]

TERMINAL_IMPORT_STATES = frozenset({"SUBMITTED", "CANCELLED", "FAILED"})
