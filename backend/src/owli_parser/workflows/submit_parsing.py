"""SubmitParsingCommand：解析任务提交编排。

流程（文档第五部分 §8.3）：
1. 事务外：校验输入、解析生效配置引用；
2. PG 事务内：检查授权与预期文档 revision；
3. catalog 登记候选 version，jobs 登记任务，artifacts 建立引用；
4. 同事务写 outbox、desired_version，提交；
5. 返回任务 ID；派发器独立投递（本 workflow 只登记 outbox，不直接投递）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import select

from owli_parser.bootstrap.settings import AppSettings
from owli_parser.core.clock import Clock
from owli_parser.core.context import ActorKind
from owli_parser.core.errors import ConflictError, NotFoundError
from owli_parser.infrastructure.uow import UnitOfWork
from owli_parser.modules.artifacts.service import ArtifactService
from owli_parser.modules.catalog.service import CatalogScope, CatalogService
from owli_parser.modules.jobs.service import JobService
from owli_parser.modules.parsing import pipeline as pipeline_module
from owli_parser.modules.parsing.builtin_adapters import default_registry
from owli_parser.ports.outbox import OutboxRecorder


@dataclass(frozen=True, slots=True)
class SubmitResult:
    job_id: uuid.UUID
    version_id: uuid.UUID
    document_id: uuid.UUID
    pool: str
    effective_config_digest: str


class SubmitParsingWorkflow:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        settings: AppSettings,
        catalog: CatalogService,
        jobs: JobService,
        artifacts: ArtifactService,
        outbox: OutboxRecorder,
        configuration: Any | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._settings = settings
        self._catalog = catalog
        self._jobs = jobs
        self._configuration = configuration
        self._artifacts = artifacts
        self._outbox = outbox

    def compile_default_pipeline(self) -> pipeline_module.CompiledPipeline:
        yaml_text = pipeline_module.load_default_pipeline_yaml()
        return pipeline_module.compile_pipeline(yaml_text, set(default_registry().keys()))

    async def resolve_pipeline(
        self, tenant_id: uuid.UUID, pipeline_ref: str | None
    ) -> pipeline_module.CompiledPipeline:
        """解析提交使用的 pipeline：租户 active 配置优先（按引用名或默认名），否则内置默认。

        配置在提交时编译并冻结进任务快照（yaml_text），worker 只按快照执行。
        """
        default = self.compile_default_pipeline()
        wanted = pipeline_ref or default.document.name
        if self._configuration is not None:
            config = await self._configuration.get_active_pipeline(tenant_id, wanted)
            if config is not None:
                return pipeline_module.compile_pipeline(config.yaml_text, set(default_registry().keys()))
        if pipeline_ref not in (None, default.document.name):
            raise NotFoundError(f"pipeline {pipeline_ref} 不存在")
        return default

    async def resolve_pipeline_with_experiment(
        self,
        tenant_id: uuid.UUID,
        pipeline_ref: str | None,
        *,
        bucket_key: str,
    ) -> tuple[pipeline_module.CompiledPipeline, dict[str, Any] | None]:
        """基础 pipeline + 可选 A/B 变体（按 bucket_key 稳定分桶）。

        实验仅作用于解析后的 pipeline 名称；无实验/暂停/变体不可编译时回退基础配置。
        """
        compiled = await self.resolve_pipeline(tenant_id, pipeline_ref)
        if self._configuration is None:
            return compiled, None
        selected = await self._configuration.select_pipeline_experiment(
            tenant_id, compiled.document.name, bucket_key=bucket_key
        )
        if selected is None:
            return compiled, None
        return cast(tuple[pipeline_module.CompiledPipeline, dict[str, Any]], selected)

    async def submit(
        self,
        scope: CatalogScope,
        *,
        artifact_id: uuid.UUID,
        dataset_id: uuid.UUID,
        pipeline_ref: str | None,
        document_id: uuid.UUID | None,
        expected_revision: int | None,
        parse_only: bool = False,
        filename: str | None = None,
    ) -> SubmitResult:
        # 1. 事务外：校验 artifact 安全状态与生效配置
        artifact = await self._artifacts.get_artifact(scope.tenant_id, artifact_id)
        if artifact.kind != "source":
            raise ConflictError("只能对原件 artifact 提交解析")
        if scope.kind is ActorKind.APPLICATION and "read" not in scope.app_actions(dataset_id):
            raise NotFoundError("数据集不存在或不可用")
        compiled, experiment = await self.resolve_pipeline_with_experiment(
            scope.tenant_id, pipeline_ref, bucket_key=str(artifact_id)
        )
        # 快照冻结完整 YAML：worker 只按快照执行（配置后续变化不影响在途任务）
        execution_plan: dict[str, Any] = {
            **compiled.execution_plan(),
            "yaml_text": compiled.yaml_text,
        }
        if experiment is not None:
            execution_plan["experiment"] = experiment
        digest = compiled.digest
        actor_id = scope.actor_id or (scope.user_ctx.user_id if scope.user_ctx else None)
        if actor_id is None:
            raise ConflictError("缺少执行主体")

        # 2-4. PG 事务内：登记 version/job/outbox 与 desired_version
        doc_info, version_info = await self._catalog.create_document_with_version(
            scope,
            dataset_id=dataset_id,
            # 原始文件名影响预检与 adapter 路由（扩展名）：由调用方提供；
            # 缺失时回落到占位名（不可由用户提交路径，服务端仅取 basename）
            filename=_safe_basename(filename) if filename else _filename_from(artifact),
            mime=artifact.mime,
            size_bytes=artifact.size_bytes,
            content_hash=artifact.content_hash,
            source_artifact_id=artifact_id,
            effective_config_digest=digest,
            chunk_policy={
                "target_tokens": self._settings.chunk.target_tokens,
                "max_tokens": self._settings.chunk.max_tokens,
                "overlap_tokens": self._settings.chunk.overlap_tokens,
                "tokenizer_ref": self._settings.chunk.tokenizer_ref,
            },
            tokenizer_revision=self._settings.chunk.tokenizer_ref,
            actor_id=actor_id,
            document_id=document_id,
            expected_revision=expected_revision,
        )
        pool = "cpu"
        async with self._uow.begin() as session:
            # artifact 引用计数（版本引用）
            await self._artifacts.increment_ref(session, artifact_id)
            job_id = await self._jobs.create_job(
                session,
                tenant_id=scope.tenant_id,
                actor_kind=str(scope.kind),
                actor_id=actor_id,
                credential_id=None,
                document_id=doc_info.id,
                version_id=version_info.id,
                dataset_id=dataset_id,
                execution_plan=execution_plan,
                effective_config_digest=digest,
                pool=pool,
                parse_only=parse_only,
            )
            await self._outbox.record(
                aggregate_type="parsing_job",
                aggregate_id=str(job_id),
                event_type="job.route",
                payload={"job_id": str(job_id)},
                session=session,
            )
        return SubmitResult(
            job_id=job_id,
            version_id=version_info.id,
            document_id=doc_info.id,
            pool=pool,
            effective_config_digest=digest,
        )

    async def submit_from_import(self, import_id: uuid.UUID) -> uuid.UUID:
        """URL/对象导入 READY 后登记解析任务（服务端主体执行）。

        以 SYSTEM 主体复用 submit()：绕过用户角色判定，但租户与数据集
        归属仍由 catalog 查询条件强制。
        """
        from owli_parser.modules.ingestion.models import SourceImportRow

        async with self._uow.read_only() as session:
            row = (
                await session.execute(select(SourceImportRow).where(SourceImportRow.id == import_id))
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("导入任务不存在")
            if row.artifact_id is None:
                raise ConflictError("导入尚未生成 artifact")
            if row.parsing_job_id is not None:
                return row.parsing_job_id
            scope = CatalogScope(
                tenant_id=row.tenant_id,
                kind=ActorKind.SYSTEM,
                actor_id=row.actor_id,
                user_ctx=None,
                application_dataset_actions=None,
            )
            artifact_id = row.artifact_id
            dataset_id = row.dataset_id
            pipeline_ref = row.pipeline_ref
            document_id = row.document_id
            expected_revision = row.expected_revision
            display_filename = row.filename_hint or (
                row.source_display_path.rsplit("/", 1)[-1] if row.source_display_path else None
            )
        result = await self.submit(
            scope,
            artifact_id=artifact_id,
            dataset_id=dataset_id,
            pipeline_ref=pipeline_ref,
            document_id=document_id,
            expected_revision=expected_revision,
            filename=display_filename,
        )
        return result.job_id


def _filename_from(artifact: object) -> str:
    aid = getattr(artifact, "id", "unknown")
    return f"document-{aid}"


def _safe_basename(filename: str) -> str:
    """只保留清洗后的 basename（不信任调用方提交的路径）。"""
    from owli_parser.modules.ingestion.preflight import sanitize_display_filename

    return sanitize_display_filename(filename)
