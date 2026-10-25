"""Queue 端口：任务派发。

- PG 事务写 job 与 outbox；派发器补偿消息丢失；broker 不是状态数据库。
- 队列语义为至少一次；PG checkpoint/fence 去重。
- 队列只传 ID/有限元信息，worker 从权威库加载。
"""

from __future__ import annotations

from typing import Protocol


class TaskDispatcher(Protocol):
    """将轻量任务消息投递到 broker 的对应队列。"""

    def enqueue_route(self, job_id: str) -> None: ...

    def enqueue_run(self, job_id: str, pool: str) -> None: ...

    def enqueue_post(self, job_id: str) -> None: ...

    def enqueue_import(self, import_id: str) -> None: ...

    def enqueue_index_action(self, action_id: str) -> None:
        """派发单条重索引行动（编辑派生；消费者按 PG 当前 revision 重建）。"""
        ...


class DispatchFailed(Exception):
    """派发失败。调用方不得回滚已提交的 PG 状态；由 outbox 重试补偿。"""
