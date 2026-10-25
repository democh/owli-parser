"""Celery/Valkey broker 配置与任务派发实现（不依赖 bootstrap，参数显式传入）。

- 队列语义为至少一次：晚确认、限制 prefetch，以 PG checkpoint/fence 去重。
- broker 不是状态数据库；派发失败由 outbox 补偿（PG 已提交状态不回滚）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from owli_parser.ports.queue import DispatchFailed


@dataclass(frozen=True, slots=True)
class BrokerConfig:
    broker_url: str
    prefetch_multiplier: int = 1
    visibility_timeout_seconds: int = 3600
    default_queue: str = "q.cpu"


@dataclass(frozen=True, slots=True)
class QueueNames:
    route: str = "q.route"
    cpu: str = "q.cpu"
    gpu: str = "q.gpu"
    post: str = "q.post"
    dlq: str = "q.dlq"


def create_celery_app(config: BrokerConfig) -> Any:
    from celery import Celery

    app = Celery("owli_parser", broker=config.broker_url, backend=None)
    app.conf.update(
        task_acks_late=True,
        worker_prefetch_multiplier=config.prefetch_multiplier,
        broker_transport_options={"visibility_timeout": config.visibility_timeout_seconds},
        task_default_queue=config.default_queue,
        task_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        task_ignore_result=True,  # 事实源是 PG，不把 Celery result 当权威
    )
    return app


class CeleryTaskDispatcher:
    """TaskDispatcher 实现：按事件类型投递到对应队列。"""

    def __init__(self, celery_app: Any, queues: QueueNames | None = None) -> None:
        self._app = celery_app
        self._queues = queues or QueueNames()

    def enqueue_route(self, job_id: str) -> None:
        try:
            self._app.send_task("owli.route_job", args=[job_id], queue=self._queues.route)
        except Exception as exc:
            raise DispatchFailed(f"route 任务派发失败: {exc.__class__.__name__}") from exc

    def enqueue_run(self, job_id: str, pool: str) -> None:
        queue = self._queues.cpu if pool == "cpu" else self._queues.gpu
        try:
            self._app.send_task("owli.run_job", args=[job_id], queue=queue)
        except Exception as exc:
            raise DispatchFailed(f"run 任务派发失败: {exc.__class__.__name__}") from exc

    def enqueue_post(self, job_id: str) -> None:
        try:
            self._app.send_task("owli.run_job", args=[job_id], queue=self._queues.post)
        except Exception as exc:
            raise DispatchFailed(f"post 任务派发失败: {exc.__class__.__name__}") from exc

    def enqueue_import(self, import_id: str) -> None:
        try:
            self._app.send_task("owli.download_import", args=[import_id], queue=self._queues.cpu)
        except Exception as exc:
            raise DispatchFailed(f"import 任务派发失败: {exc.__class__.__name__}") from exc

    def enqueue_index_action(self, action_id: str) -> None:
        try:
            self._app.send_task("owli.index_action", args=[action_id], queue=self._queues.cpu)
        except Exception as exc:
            raise DispatchFailed(f"索引行动派发失败: {exc.__class__.__name__}") from exc


def broker_config_from_settings(settings: Any) -> BrokerConfig:
    """由 bootstrap 侧把 AppSettings 转换为 BrokerConfig（本模块不认识 settings 类型）。"""
    return BrokerConfig(
        broker_url=settings.broker.url,
        prefetch_multiplier=settings.broker.worker_prefetch_multiplier,
        visibility_timeout_seconds=settings.broker.visibility_timeout_seconds,
        default_queue=settings.jobs.queues.cpu,
    )


def queue_names_from_settings(settings: Any) -> QueueNames:
    return QueueNames(
        route=settings.jobs.queues.route,
        cpu=settings.jobs.queues.cpu,
        gpu=settings.jobs.queues.gpu,
        post=settings.jobs.queues.post,
        dlq=settings.jobs.queues.dlq,
    )
