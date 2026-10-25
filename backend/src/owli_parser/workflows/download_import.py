"""DownloadImport：URL/存储对象导入的下载编排（worker）。

状态流：PENDING → FETCHING → VERIFYING → READY → SUBMITTED。
- 下载与解析分进程：本流程仅执行安全下载与 artifact 登记。
- READY 后以 PG 事务登记解析任务（submit_from_import），
  parsing_job_id 唯一，重复消息不能登记第二个解析任务。
- 失败分类：可重试且预算内 → RETRY_WAIT + outbox 重派；否则 FAILED。
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from owli_parser.bootstrap.settings import AppSettings
from owli_parser.core.clock import Clock
from owli_parser.infrastructure.uow import UnitOfWork
from owli_parser.modules.ingestion.service import ImportService, wrap_fetch_errors
from owli_parser.ports.outbox import OutboxRecorder
from owli_parser.ports.secrets import SecretResolver, SecretUnavailable
from owli_parser.workflows.submit_parsing import SubmitParsingWorkflow


class DownloadImportWorkflow:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        settings: AppSettings,
        imports: ImportService,
        submit_parsing: SubmitParsingWorkflow,
        outbox: OutboxRecorder,
        secrets: SecretResolver,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._settings = settings
        self._imports = imports
        self._submit_parsing = submit_parsing
        self._outbox = outbox
        self._secrets = secrets

    def execute(self, import_id: uuid.UUID, worker_id: str) -> None:
        asyncio.run(self._execute(import_id, worker_id))

    async def _execute(self, import_id: uuid.UUID, worker_id: str) -> None:
        fence = await self._imports.claim_for_download(import_id, worker_id)
        if fence is None:
            return
        try:
            url = await self._resolve_url(import_id)
            result = await asyncio.to_thread(self._imports.download_to_temp, import_id, url)
            artifact_id = await self._imports.register_downloaded_artifact(
                import_id,
                fence,
                content_hash=result.sha256,
                size_bytes=result.size_bytes,
                content_type_hint=result.content_type_hint,
            )
            if artifact_id is None:
                return  # 旧 fence：丢弃结果
            await self._imports.mark_ready(import_id, fence, artifact_id)
            job_id = await self._submit_parsing.submit_from_import(import_id)
            await self._imports.mark_submitted(import_id, job_id)
            async with self._uow.begin() as session:
                await self._outbox.record(
                    aggregate_type="parsing_job",
                    aggregate_id=str(job_id),
                    event_type="job.route",
                    payload={"job_id": str(job_id)},
                    session=session,
                )
        except Exception as exc:
            retryable, code, message = wrap_fetch_errors(exc)
            await self._handle_failure(import_id, fence, retryable, code, message)

    async def _resolve_url(self, import_id: uuid.UUID) -> str:
        from owli_parser.modules.ingestion.models import SourceImportRow

        async with self._uow.read_only() as session:
            row = (
                await session.execute(select(SourceImportRow).where(SourceImportRow.id == import_id))
            ).scalar_one()
            if row.source_kind != "url":
                raise RuntimeError("storage_object 导入尚未在本阶段启用")
            if not row.source_secret_ref:
                raise SecretUnavailable("来源引用已回收")
            ref = row.source_secret_ref
        return self._imports.load_source_url(import_id, ref)

    async def _handle_failure(
        self,
        import_id: uuid.UUID,
        fence: int,
        retryable: bool,
        code: str,
        message: str,
    ) -> None:
        from owli_parser.modules.ingestion.models import SourceImportRow

        async with self._uow.read_only() as session:
            row = (
                await session.execute(select(SourceImportRow).where(SourceImportRow.id == import_id))
            ).scalar_one()
            attempt = row.attempt
        budget_left = attempt < self._settings.jobs.max_attempts
        if retryable and budget_left:
            await self._imports.release_lease(
                import_id, fence, state="RETRY_WAIT", error={"code": code, "message": message}
            )
            async with self._uow.begin() as session:
                await self._outbox.record(
                    aggregate_type="source_import",
                    aggregate_id=str(import_id),
                    event_type="import.retry",
                    payload={"import_id": str(import_id)},
                    session=session,
                )
            return
        await self._imports.release_lease(
            import_id, fence, state="FAILED", error={"code": code, "message": message}
        )
