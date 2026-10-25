"""RunParsingJob：worker 内的解析执行编排（纵向闭环）。

阶段流：ROUTING → PARSING → NORMALIZING → CHUNKING → INDEXING → PUBLISHING
→ DONE/PARTIAL_DONE。每阶段：
- 检查取消；检查 fence；写 checkpoint（JobStep）。
- 网络与推理在事务外；PG 提交使用 CAS。
- 失败按分类处理：retryable 且预算内 → RETRY_WAIT + outbox 重派；否则 FAILED/DEAD_LETTER。
- 部分发布：默认不自动发布；仅当数据集 allow_partial 且 coverage≥阈值 才发布为
  PARTIAL_DONE；否则 FAILED(coverage_insufficient)，缺失单位在 coverage 报告中展示。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from owli_parser.bootstrap.settings import AppSettings
from owli_parser.contracts.events import JobState
from owli_parser.core.clock import Clock, ensure_utc
from owli_parser.core.errors import CapabilityNotReadyError
from owli_parser.infrastructure.telemetry import METRICS
from owli_parser.infrastructure.uow import UnitOfWork
from owli_parser.modules.artifacts.service import ArtifactInfo, ArtifactService
from owli_parser.modules.catalog.models import DatasetRow, DocumentRow, DocVersionRow
from owli_parser.modules.catalog.service import CatalogService
from owli_parser.modules.jobs import domain as jobs_domain
from owli_parser.modules.jobs.models import ParsingJobRow
from owli_parser.modules.jobs.service import JobInfo, JobService
from owli_parser.modules.knowledge.review import collect_review_targets
from owli_parser.modules.knowledge.service import KnowledgeService
from owli_parser.modules.parsing import normalize
from owli_parser.modules.parsing import pipeline as pipeline_module
from owli_parser.modules.parsing import probe as probe_module
from owli_parser.modules.parsing.adapters_base import (
    ExecutionContext,
    ParseCancelledError,
    ParseFailedError,
    ParseOutcome,
    SourceHandle,
)
from owli_parser.modules.parsing.builtin_adapters import default_registry
from owli_parser.modules.parsing.pipeline import (
    CompiledPipeline,
    detect_format,
    route_first_success,
)
from owli_parser.modules.retrieval.indexing import IndexPublicationService
from owli_parser.ports.outbox import OutboxRecorder


class JobExecutionError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class RunParsingJobWorkflow:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        settings: AppSettings,
        session_factory: async_sessionmaker[AsyncSession],
        catalog: CatalogService,
        jobs: JobService,
        artifacts: ArtifactService,
        knowledge: KnowledgeService,
        outbox: OutboxRecorder,
        index_publication: IndexPublicationService | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._settings = settings
        self._session_factory = session_factory
        self._catalog = catalog
        self._jobs = jobs
        self._artifacts = artifacts
        self._knowledge = knowledge
        self._outbox = outbox
        self._index_publication = index_publication

    # ------------------------------------------------------------------
    # 入口（Celery worker 同步调用，内部 asyncio.run）
    # ------------------------------------------------------------------

    def execute(self, job_id: uuid.UUID, worker_id: str, pipeline: CompiledPipeline | None = None) -> None:
        asyncio.run(self._execute(job_id, worker_id, pipeline))

    async def _execute(self, job_id: uuid.UUID, worker_id: str, pipeline: CompiledPipeline | None) -> None:
        await self._observe_queue_wait(job_id)
        fence = await self._jobs.acquire_lease(job_id, worker_id)
        if fence is None:
            return  # 已被其他 worker 领取或不可领取
        try:
            if pipeline is None:
                pipeline = await self._compile_from_snapshot(job_id)
            await self._run_stages(job_id, fence, pipeline)
        except ParseCancelledError:
            await self._jobs.finalize_cancelled(job_id, fence)
        except JobExecutionError as exc:
            await self._handle_failure(job_id, fence, exc)
        except Exception as exc:
            await self._handle_failure(
                job_id, fence, JobExecutionError("internal_error", exc.__class__.__name__, retryable=False)
            )

    async def _compile_from_snapshot(self, job_id: uuid.UUID) -> CompiledPipeline:
        """从任务快照重建 pipeline（路由只读冻结快照）；快照缺失回退默认配置。"""
        job = await self._jobs.get_job_by_id(job_id)
        yaml_text = None
        if job is not None:
            plan = job.execution_plan or {}
            candidate = plan.get("yaml_text") if isinstance(plan, dict) else None
            if isinstance(candidate, str) and candidate.strip():
                yaml_text = candidate
        if yaml_text is None:
            yaml_text = pipeline_module.load_default_pipeline_yaml()
        return pipeline_module.compile_pipeline(yaml_text, set(default_registry().keys()))

    # ------------------------------------------------------------------
    # 阶段执行
    # ------------------------------------------------------------------

    async def _run_stages(self, job_id: uuid.UUID, fence: int, pipeline: CompiledPipeline) -> None:
        await self._check_cancel(job_id)
        job = await self._jobs.get_job_by_id(job_id)
        if job is None:
            return

        # ---- ROUTING：下载原件到隔离临时目录 + 预检 + 路由 ----
        with tempfile.TemporaryDirectory(prefix="owli-parse-") as tmp_dir:
            source_path, filename, content_hash = await self._materialize_source(
                job.tenant_id, job.version_id, Path(tmp_dir)
            )
            handle = SourceHandle(
                path=source_path,
                mime=None,
                filename=filename,
                content_hash=content_hash,
                artifact_id="",
            )
            features = await asyncio.to_thread(probe_module.probe, handle)
            fmt = detect_format(features)
            registry = default_registry()
            supports = {name: adapter.supports(features).supported for name, adapter in registry.items()}
            decision = route_first_success(pipeline, fmt, features, supports)
            assert decision.adapter_name is not None  # route_first_success 已拒绝 None
            adapter = registry[decision.adapter_name]
            await self._jobs.transition(
                job_id,
                fence,
                JobState.PARSING.value,
                stage=JobState.PARSING.value,
                progress={"format": fmt, "adapter": adapter.name},
                total_units=features.page_count,
                event_detail={"route": decision.reason, "skipped": decision.skipped},
            )
            stage_started = self._clock.now()

            # ---- PARSING：同步 adapter 在线程池执行 ----
            deadline = self._clock.now().timestamp() + self._settings.jobs.stage_deadline_seconds
            execution = ExecutionContext(
                deadline_epoch=deadline,
                cancellation=lambda: False,  # 取消由阶段间检查实现（进程内解析不可中断）
            )

            def _progress(done: int, total: int) -> None:
                del done, total  # 进度在阶段结束时统一提交（避免高频事务）

            execution = ExecutionContext(
                deadline_epoch=deadline,
                cancellation=lambda: False,
                progress=_progress,
            )
            parse_options: dict[str, object] = {
                "archive_max_entries": self._settings.parsing.archive_max_entries,
                "archive_max_depth": self._settings.parsing.archive_max_depth,
                "archive_max_ratio": self._settings.parsing.archive_max_ratio,
            }
            try:
                outcome: ParseOutcome = await asyncio.to_thread(
                    adapter.parse,
                    handle,
                    parse_options,
                    execution,
                )
            except ParseCancelledError:
                raise
            except ParseFailedError as exc:
                raise JobExecutionError(exc.code, str(exc), retryable=exc.retryable) from exc
            except Exception as exc:
                raise JobExecutionError("parse_failed", exc.__class__.__name__, retryable=False) from exc
            self._observe_stage("parsing", stage_started)
            await self._record_parse_steps(job, outcome)

        await self._check_cancel(job_id)
        await self._jobs.transition(
            job_id,
            fence,
            JobState.NORMALIZING.value,
            stage=JobState.NORMALIZING.value,
            total_units=outcome.total_units,
            completed_units=outcome.succeeded_units,
            coverage=(outcome.succeeded_units / outcome.total_units if outcome.total_units else 1.0),
        )

        # ---- NORMALIZING：ParseOutcome -> DocumentIR（保存为不可变 IR artifact）----
        stage_started = self._clock.now()
        version = await self._get_version(job.version_id)
        desired_version_id, allow_partial = await self._publish_context(job.document_id, job.dataset_id)
        ir = normalize.build_document_ir(
            parse_outcome=outcome,
            adapter_name=adapter.name,
            adapter_version=adapter.version,
            adapter_mode=adapter.mode,
            doc_id=job.document_id,
            version_id=job.version_id,
            dataset_id=job.dataset_id,
            tenant_id=job.tenant_id,
            content_hash=content_hash or "",
            effective_config_digest=job.effective_config_digest,
            source_artifact_id=version.source_artifact_id,
        )
        ir_bytes = json.dumps(ir.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")
        ir_artifact = await self._store_bytes(
            job.tenant_id, job.dataset_id, kind="ir", mime="application/json", data=ir_bytes
        )
        await self._catalog.set_version_ir(
            job.version_id,
            ir_artifact_id=ir_artifact.id,
            coverage=ir.coverage.model_dump(mode="json"),
            quality_report=ir.quality.model_dump(mode="json"),
            lifecycle="parsed",
        )
        self._observe_stage("normalizing", stage_started)

        # ---- CHUNKING ----
        await self._check_cancel(job_id)
        await self._jobs.transition(job_id, fence, JobState.CHUNKING.value, stage=JobState.CHUNKING.value)
        stage_started = self._clock.now()
        chunk_ids = await self._knowledge.create_chunks_for_version(
            tenant_id=job.tenant_id,
            dataset_id=job.dataset_id,
            doc_id=job.document_id,
            version_id=job.version_id,
            ir=ir,
            created_by_kind="system",
            created_by_id=job.actor_id,
        )
        self._observe_stage("chunking", stage_started)

        # ---- REVIEW：低置信块与缺失单位登记复核（幂等重建；未评估置信度不算低置信）----
        await self._check_cancel(job_id)
        review_targets = collect_review_targets(
            blocks=ir.blocks,
            missing_units=list(ir.coverage.missing_units or []),
            low_confidence_threshold=self._settings.jobs.review_low_confidence_threshold,
        )
        if review_targets.missing_units or review_targets.low_confidence_block_ids:
            async with self._uow.begin() as session:
                if review_targets.missing_units:
                    await self._knowledge.enqueue_review_items(
                        tenant_id=job.tenant_id,
                        version_id=job.version_id,
                        job_id=job_id,
                        block_ids=review_targets.missing_units,
                        reason="missing_unit",
                        session=session,
                    )
                if review_targets.low_confidence_block_ids:
                    await self._knowledge.enqueue_review_items(
                        tenant_id=job.tenant_id,
                        version_id=job.version_id,
                        job_id=job_id,
                        block_ids=review_targets.low_confidence_block_ids,
                        reason="low_confidence",
                        session=session,
                    )

        # ---- INDEXING：写入已启用检索通道并确认 manifest；无必需通道则明确 retrieval_ready=false ----
        await self._check_cancel(job_id)
        await self._jobs.transition(job_id, fence, JobState.INDEXING.value, stage=JobState.INDEXING.value)
        stage_started = self._clock.now()
        required_channels: list[str] = []
        if self._settings.retrieval.enabled:
            if self._index_publication is None:
                raise JobExecutionError(
                    "capability_not_ready", "检索已启用但索引发布器未装配", retryable=False
                )
            try:
                required_channels = await self._index_publication.required_channels(job.tenant_id)
            except CapabilityNotReadyError as exc:
                raise JobExecutionError("capability_not_ready", str(exc), retryable=False) from exc
        manifest = await self._knowledge.create_manifest(
            tenant_id=job.tenant_id,
            version_id=job.version_id,
            index_generation=1,
            required_channels=required_channels,
        )
        retrieval_ready = False
        if required_channels:
            publish_outcome = await self._index_publication.publish_version(  # type: ignore[union-attr]
                tenant_id=job.tenant_id,
                version_id=job.version_id,
                generation=1,
                manifest=manifest,
            )
            retrieval_ready = publish_outcome.state == "ready"
        await self._jobs.transition(
            job_id,
            fence,
            JobState.PUBLISHING.value,
            stage=JobState.PUBLISHING.value,
            progress={"chunks": len(chunk_ids), "manifest": str(manifest.id)},
            retrieval_ready=retrieval_ready,
        )
        self._observe_stage("indexing", stage_started)

        # ---- PUBLISHING：CAS 发布可见版本 ----
        stage_started = self._clock.now()
        coverage_value = outcome.succeeded_units / outcome.total_units if outcome.total_units else 1.0
        partial_ok = coverage_value >= self._settings.jobs.partial_publish_min_coverage
        if coverage_value < 1.0 and not (allow_partial and partial_ok):
            raise JobExecutionError(
                "coverage_insufficient",
                f"覆盖率 {coverage_value:.2f}，未允许部分发布",
                retryable=False,
            )
        published = await self._catalog.publish_version(
            job.tenant_id,
            document_id=job.document_id,
            version_id=job.version_id,
            expected_desired_version=desired_version_id,
        )
        if not published:
            raise JobExecutionError("publish_superseded", "发布时 desired_version 已变化", retryable=False)
        final_state = JobState.DONE.value if coverage_value >= 1.0 else JobState.PARTIAL_DONE.value
        await self._jobs.transition(
            job_id,
            fence,
            final_state,
            stage=final_state,
            coverage=coverage_value,
            retrieval_ready=retrieval_ready,
            event_detail={"published_version": str(job.version_id)},
        )
        self._observe_stage("publishing", stage_started)

    async def _record_parse_steps(self, job: JobInfo, outcome: ParseOutcome) -> None:
        """按页/文档单元登记解析步骤（供续跑与运维查询；每 attempt 一套）。"""
        failed_units = {error.unit_id for error in outcome.unit_errors}
        if outcome.unit_kind == "document" or outcome.total_units <= 0:
            units = ["document"]
        else:
            prefix = "page" if outcome.unit_kind == "page" else "time_window"
            units = [f"{prefix}:{index}" for index in range(outcome.total_units)]
        checkpoint = {"engine": outcome.engine, "engine_version": outcome.engine_version}
        async with self._uow.begin() as session:
            for unit_id in units:
                failed = unit_id in failed_units
                await self._jobs.record_step(
                    session,
                    job_id=job.id,
                    tenant_id=job.tenant_id,
                    stage=JobState.PARSING.value,
                    unit_id=unit_id,
                    attempt=job.attempt,
                    state="failed" if failed else "succeeded",
                    checkpoint_ref=checkpoint,
                    error_code="parse_unit_failed" if failed else None,
                )

    async def _observe_queue_wait(self, job_id: uuid.UUID) -> None:
        """排队等待（入队到被领取，含重试等待）；行不存在时跳过。"""
        async with self._uow.read_only() as session:
            created_at = (
                await session.execute(select(ParsingJobRow.created_at).where(ParsingJobRow.id == job_id))
            ).scalar_one_or_none()
        if created_at is None:
            return
        waiting = (self._clock.now() - ensure_utc(created_at)).total_seconds()
        METRICS.observe("owli_stage_duration_seconds", waiting, {"stage": "queue"})

    def _observe_stage(self, stage: str, started: datetime) -> None:
        elapsed = (self._clock.now() - ensure_utc(started)).total_seconds()
        METRICS.observe("owli_stage_duration_seconds", elapsed, {"stage": stage})

    async def _materialize_source(
        self, tenant_id: uuid.UUID, version_id: uuid.UUID, tmp_dir: Path
    ) -> tuple[Path, str, str]:
        """从存储读取原件到本地临时文件（解析器默认无网络）。"""
        async with self._uow.read_only() as session:
            version_row = (
                await session.execute(select(DocVersionRow).where(DocVersionRow.id == version_id))
            ).scalar_one()
            source_artifact_id = version_row.source_artifact_id
            document = (
                await session.execute(select(DocumentRow).where(DocumentRow.id == version_row.document_id))
            ).scalar_one()
            filename = document.filename
            content_hash = document.content_hash or ""
        location, profile = await self._artifacts.resolve_location(tenant_id, source_artifact_id)

        dest = tmp_dir / "source.bin"
        suffix = Path(filename).suffix.lower()
        if suffix:
            dest = tmp_dir / f"source{suffix}"

        def _copy() -> None:
            reader = self._artifacts.open_reader(location, profile)
            try:
                with open(dest, "wb") as out:
                    while True:
                        chunk = reader.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
            finally:
                reader.close()

        await asyncio.to_thread(_copy)
        return dest, filename, content_hash

    async def _store_bytes(
        self,
        tenant_id: uuid.UUID,
        dataset_id: uuid.UUID,
        *,
        kind: str,
        mime: str,
        data: bytes,
    ) -> ArtifactInfo:
        from owli_parser.core.crypto import sha256_hex

        async with self._uow.read_only() as session:
            dataset = (
                await session.execute(select(DatasetRow).where(DatasetRow.id == dataset_id))
            ).scalar_one()
        profile = await self._artifacts._router.resolve_for_dataset(tenant_id, dataset)
        return await self._artifacts.store_object(
            tenant_id,
            kind=kind,
            mime=mime,
            content_hash=sha256_hex(data),
            size_bytes=len(data),
            profile=profile,
            chunks_factory=lambda: iter([data]),
            content_type=mime,
        )

    async def _get_version(self, version_id: uuid.UUID) -> DocVersionRow:
        async with self._uow.read_only() as session:
            return (
                await session.execute(select(DocVersionRow).where(DocVersionRow.id == version_id))
            ).scalar_one()

    async def _publish_context(
        self, document_id: uuid.UUID, dataset_id: uuid.UUID
    ) -> tuple[uuid.UUID | None, bool]:
        """读取发布前置条件：desired_version_id（文档）与 allow_partial（数据集）。"""
        async with self._uow.read_only() as session:
            doc = (
                await session.execute(select(DocumentRow).where(DocumentRow.id == document_id))
            ).scalar_one()
            dataset = (
                await session.execute(select(DatasetRow).where(DatasetRow.id == dataset_id))
            ).scalar_one()
            return doc.desired_version_id, dataset.allow_partial

    async def _check_cancel(self, job_id: uuid.UUID) -> None:
        if await self._jobs.is_cancel_requested(job_id):
            raise ParseCancelledError()

    async def _handle_failure(self, job_id: uuid.UUID, fence: int, exc: JobExecutionError) -> None:
        job = await self._jobs.get_job_by_id(job_id)
        if job is None:
            return
        category = jobs_domain.classify_failure(exc.code)
        retryable = exc.retryable and category == "retryable"
        max_attempts = self._settings.jobs.max_attempts
        error = {"code": exc.code, "message": str(exc)}
        if retryable and job.attempt < max_attempts:
            await self._jobs.transition(
                job_id,
                fence,
                JobState.RETRY_WAIT.value,
                error=error,
                event_detail={"retryable": True, "attempt": job.attempt},
            )
            async with self._uow.begin() as session:
                await self.outbox_record_retry(session, job_id)
            return
        target = (
            JobState.DEAD_LETTER.value
            if (retryable and job.attempt >= max_attempts)
            else JobState.FAILED.value
        )
        await self._jobs.transition(job_id, fence, target, error=error)

    async def outbox_record_retry(self, session: object, job_id: uuid.UUID) -> None:
        await self._outbox.record(
            aggregate_type="parsing_job",
            aggregate_id=str(job_id),
            event_type="job.retry",
            payload={"job_id": str(job_id)},
            session=session,
        )

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory
