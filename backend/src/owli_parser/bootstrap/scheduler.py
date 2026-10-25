"""scheduler：outbox 派发与租约回收循环（首期与控制进程合并部署）。

- outbox 派发：扫描 pending -> 按事件类型投递 -> 标记 delivered；
  投递失败保留 pending 并指数退避，broker 不是状态数据库。
- 租约回收：运行阶段 + lease 过期的任务置 RETRY_WAIT 并重新派发。
- 孤儿产物清理：pending location 超时（写入中断）与过期上传会话。
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from datetime import datetime, timedelta

from sqlalchemy import select

from owli_parser.bootstrap.container import Container
from owli_parser.core.clock import SystemClock
from owli_parser.infrastructure.broker import (
    CeleryTaskDispatcher,
    create_celery_app,
)
from owli_parser.infrastructure.crosscut_models import OutboxEventRow
from owli_parser.infrastructure.telemetry import configure_logging, get_logger
from owli_parser.modules.retention.service import RetentionReport

_logger = get_logger("owli.scheduler")


class Scheduler:
    def __init__(self, container: Container) -> None:
        self.container = container
        self.clock = SystemClock()
        from owli_parser.infrastructure.broker import (
            broker_config_from_settings,
            queue_names_from_settings,
        )

        self.dispatcher = CeleryTaskDispatcher(
            create_celery_app(broker_config_from_settings(container.settings)),
            queue_names_from_settings(container.settings),
        )
        self._stop = asyncio.Event()
        self._last_gc: datetime | None = None

    async def run(self) -> None:
        settings = self.container.settings
        _logger.info("scheduler_started")
        while not self._stop.is_set():
            try:
                dispatched = await self._dispatch_outbox(settings.jobs.outbox_batch_size)
                recovered = await self.container.jobs.sweep_expired_leases(
                    batch=settings.jobs.outbox_batch_size
                )
                cleaned = await self._cleanup_orphans()
                retention = await self._maybe_purge_deleted()
                if (
                    dispatched
                    or recovered
                    or cleaned
                    or (retention is not None and (retention.documents or retention.artifacts))
                ):
                    _logger.info(
                        "scheduler_cycle",
                        extra={
                            "dispatched": dispatched,
                            "recovered": recovered,
                            "cleaned": cleaned,
                            "purged_documents": retention.documents if retention else 0,
                            "purged_artifacts": retention.artifacts if retention else 0,
                            "retention_errors": retention.errors if retention else 0,
                        },
                    )
            except Exception as exc:
                _logger.error("scheduler_cycle_error", extra={"error": exc.__class__.__name__})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=settings.jobs.outbox_poll_seconds)
            except TimeoutError:
                continue
        _logger.info("scheduler_stopped")

    def stop(self) -> None:
        self._stop.set()

    async def _dispatch_outbox(self, batch: int) -> int:
        now = self.clock.now()
        dispatched = 0
        async with self.container.uow.begin() as session:
            rows = (
                (
                    await session.execute(
                        select(OutboxEventRow)
                        .where(
                            OutboxEventRow.delivery_state == "pending",
                            OutboxEventRow.available_at <= now,
                        )
                        .order_by(OutboxEventRow.available_at)
                        .limit(batch)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                try:
                    self._send(row.event_type, row.payload or {})
                    row.delivery_state = "delivered"
                    row.delivered_at = now
                    dispatched += 1
                except Exception as exc:
                    row.attempts += 1
                    row.last_error = exc.__class__.__name__
                    backoff = min(2 ** min(row.attempts, 8), 300)
                    row.available_at = now + timedelta(seconds=backoff)
                    if row.attempts >= 20:
                        _logger.error(
                            "outbox_dispatch_exhausted",
                            extra={"event_type": row.event_type, "aggregate_id": row.aggregate_id},
                        )
        return dispatched

    def _send(self, event_type: str, payload: dict[str, object]) -> None:
        if event_type == "job.route":
            job_id = str(payload.get("job_id", ""))
            if job_id:
                self.dispatcher.enqueue_route(job_id)
            return
        if event_type == "job.retry":
            job_id = str(payload.get("job_id", ""))
            if job_id:
                self.dispatcher.enqueue_route(job_id)
            return
        if event_type == "import.download":
            import_id = str(payload.get("import_id", ""))
            if import_id:
                self.dispatcher.enqueue_import(import_id)
            return
        if event_type == "import.retry":
            import_id = str(payload.get("import_id", ""))
            if import_id:
                self.dispatcher.enqueue_import(import_id)
            return
        if event_type == "index.apply":
            action_id = str(payload.get("action_id", ""))
            if action_id:
                self.dispatcher.enqueue_index_action(action_id)
            return
        raise ValueError(f"未知 outbox 事件类型: {event_type}")

    async def _cleanup_orphans(self) -> int:
        """清理超时 pending location 与过期上传会话（登记驱动，不扫描 bucket）。"""
        from owli_parser.modules.artifacts.models import ArtifactLocationRow, UploadRow

        settings = self.container.settings
        now = self.clock.now()
        orphan_before = now - timedelta(seconds=settings.retention.orphan_artifact_ttl_seconds)
        cleaned = 0
        async with self.container.uow.begin() as session:
            stale = (
                (
                    await session.execute(
                        select(ArtifactLocationRow)
                        .where(
                            ArtifactLocationRow.state.in_(["pending", "failed"]),
                            ArtifactLocationRow.created_at < orphan_before,
                        )
                        .limit(50)
                    )
                )
                .scalars()
                .all()
            )
            for location in stale:
                location.state = "orphaned"
                cleaned += 1
            expired_uploads = (
                (
                    await session.execute(
                        select(UploadRow).where(UploadRow.state == "initiated", UploadRow.expires_at < now)
                    )
                )
                .scalars()
                .all()
            )
            for upload in expired_uploads:
                upload.state = "expired"
                cleaned += 1
        return cleaned

    async def _maybe_purge_deleted(self) -> RetentionReport | None:
        """按 gc_interval 限频执行软删文档回收（避免每个 outbox 轮询都扫表）。"""
        settings = self.container.settings
        now = self.clock.now()
        interval = timedelta(seconds=settings.retention.gc_interval_seconds)
        if self._last_gc is not None and now - self._last_gc < interval:
            return None
        self._last_gc = now
        return await self.container.retention.purge_deleted_documents()


async def run_scheduler() -> None:
    container = Container()
    configure_logging(container.settings.observability.log_level, container.settings.observability.log_json)
    scheduler = Scheduler(container)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows 平台差异
            loop.add_signal_handler(sig, scheduler.stop)
    try:
        await scheduler.run()
    finally:
        await container.dispose()


def main() -> None:
    asyncio.run(run_scheduler())


__all__ = ["Scheduler", "main", "run_scheduler"]
