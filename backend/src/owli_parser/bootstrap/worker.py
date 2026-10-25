"""worker 进程入口与任务执行处理器。

- 任务函数只接收 ID；worker 从权威库加载数据。
- 重 CPU/GPU 同步调用在 worker 进程内执行，不阻塞任何 API 事件循环。
"""

from __future__ import annotations

import uuid
from typing import Any

from owli_parser.bootstrap.container import Container
from owli_parser.bootstrap.settings import AppSettings
from owli_parser.infrastructure.telemetry import configure_logging, get_logger

_logger = get_logger("owli.worker")
_container: Container | None = None


def get_worker_container() -> Container:
    global _container
    if _container is None:
        configure_logging()
        _container = Container()
    return _container


def handle_route_job(job_id: str) -> None:
    """路由任务：读取任务快照并派发到对应执行队列。"""
    container = get_worker_container()
    parsed_id = uuid.UUID(job_id)

    async def _route() -> None:
        job = await container.jobs.get_job_by_id(parsed_id)

        if job is None:
            return
        from owli_parser.infrastructure.broker import (
            CeleryTaskDispatcher,
            broker_config_from_settings,
            create_celery_app,
            queue_names_from_settings,
        )

        dispatcher = CeleryTaskDispatcher(
            create_celery_app(broker_config_from_settings(container.settings)),
            queue_names_from_settings(container.settings),
        )
        dispatcher.enqueue_run(job_id, job.pool)

    import asyncio

    asyncio.run(_route())


def handle_run_job(job_id: str) -> None:
    container = get_worker_container()
    worker_id = f"worker-{uuid.uuid4().hex[:8]}"
    _logger.info("run_job_start", extra={"job_id": job_id, "worker_id": worker_id})
    # pipeline=None：执行器从任务快照（yaml_text）重建，保证快照与执行一致
    container.run_parsing_job.execute(uuid.UUID(job_id), worker_id, None)
    _logger.info("run_job_done", extra={"job_id": job_id, "worker_id": worker_id})


def handle_download_import(import_id: str) -> None:
    container = get_worker_container()
    worker_id = f"worker-{uuid.uuid4().hex[:8]}"
    _logger.info("download_import_start", extra={"import_id": import_id, "worker_id": worker_id})
    container.download_import.execute(uuid.UUID(import_id), worker_id)
    _logger.info("download_import_done", extra={"import_id": import_id, "worker_id": worker_id})


def handle_index_action(action_id: str) -> None:
    """消费编辑派生的重索引行动（幂等：仅 pending 执行；失败由 outbox 重派）。"""
    import asyncio

    container = get_worker_container()
    _logger.info("index_action_start", extra={"action_id": action_id})

    async def _apply() -> None:
        result = await container.index_publication.apply_action(uuid.UUID(action_id))
        _logger.info("index_action_done", extra={"action_id": action_id, "result": result})

    asyncio.run(_apply())


def load_celery_app(settings: AppSettings) -> Any:
    """注册 celery 任务（bootstrap 层允许依赖 infrastructure 与 workflows）。"""
    from owli_parser.infrastructure.broker import broker_config_from_settings, create_celery_app

    app = create_celery_app(broker_config_from_settings(settings))

    @app.task(name="owli.route_job")  # type: ignore[untyped-decorator]
    def route_job(job_id: str) -> None:
        handle_route_job(job_id)

    @app.task(name="owli.run_job")  # type: ignore[untyped-decorator]
    def run_job(job_id: str) -> None:
        handle_run_job(job_id)

    @app.task(name="owli.download_import")  # type: ignore[untyped-decorator]
    def download_import(import_id: str) -> None:
        handle_download_import(import_id)

    @app.task(name="owli.index_action")  # type: ignore[untyped-decorator]
    def index_action(action_id: str) -> None:
        handle_index_action(action_id)

    return app


def run_worker(pool: str = "cpu") -> None:
    """启动 Celery worker；pool 决定消费的队列集合。"""
    from owli_parser.bootstrap.settings import load_boot_settings

    settings = load_boot_settings()
    app = load_celery_app(settings)
    queues = {
        "cpu": [settings.jobs.queues.route, settings.jobs.queues.cpu, settings.jobs.queues.post],
        "gpu": [settings.jobs.queues.gpu],
    }.get(pool, [settings.jobs.queues.cpu])
    configure_logging(settings.observability.log_level, settings.observability.log_json)
    _logger.info("worker_starting", extra={"pool": pool, "queues": queues})
    app.worker_main(
        argv=[
            "worker",
            f"--concurrency={settings.jobs.cpu_slots if pool == 'cpu' else max(settings.jobs.gpu_slots, 1)}",
            f"--prefetch-multiplier={settings.broker.worker_prefetch_multiplier}",
            *[f"--queues={q}" for q in queues],
            "--loglevel=INFO",
        ]
    )
