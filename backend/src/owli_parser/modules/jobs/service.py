"""jobs 应用服务：任务登记、租约、CAS 状态推进、事件流、取消与重试。

- PG 为任务权威；状态提交使用 CAS（fence + 当前状态联合条件）。
- 旧 worker 即使超时后继续返回也不得提交或发布（fence 不匹配即拒绝）。
- 事件先持久化再推送；seq 在单 job 内单调。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult

from owli_parser.contracts.events import EVENT_SCHEMA_VERSION, JobState
from owli_parser.core.clock import Clock
from owli_parser.core.errors import ConflictError, NotFoundError
from owli_parser.core.pagination import decode_cursor, encode_cursor
from owli_parser.infrastructure.uow import UnitOfWork
from owli_parser.modules.jobs import domain
from owli_parser.modules.jobs.models import JobEventRow, JobStepRow, ParsingJobRow


@dataclass(frozen=True, slots=True)
class JobInfo:
    id: uuid.UUID
    tenant_id: uuid.UUID
    actor_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    dataset_id: uuid.UUID
    state: str
    stage: str | None
    attempt: int
    pool: str
    progress: dict[str, Any]
    total_units: int | None
    completed_units: int
    coverage: float | None
    error: dict[str, Any] | None
    parse_only: bool
    retrieval_ready: bool
    fence: int
    effective_config_digest: str
    execution_plan: dict[str, Any]


@dataclass(frozen=True, slots=True)
class JobEventInfo:
    seq: int
    event_type: str
    attempt: int | None
    from_state: str | None
    to_state: str | None
    ts: Any
    detail: dict[str, Any]


@dataclass(frozen=True, slots=True)
class JobStepInfo:
    stage: str
    unit_id: str
    attempt: int
    state: str
    checkpoint_ref: dict[str, Any] | None
    error_code: str | None
    started_at: Any
    finished_at: Any


class JobService:
    def __init__(self, uow: UnitOfWork, clock: Clock, *, lease_seconds: int) -> None:
        self._uow = uow
        self._clock = clock
        self._lease_seconds = lease_seconds

    # ------------------------------------------------------------------
    # 登记与查询
    # ------------------------------------------------------------------

    async def create_job(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        actor_kind: str,
        actor_id: uuid.UUID,
        credential_id: uuid.UUID | None,
        document_id: uuid.UUID,
        version_id: uuid.UUID,
        dataset_id: uuid.UUID,
        execution_plan: dict[str, Any],
        effective_config_digest: str,
        pool: str,
        parse_only: bool,
    ) -> uuid.UUID:
        """在调用方事务内登记任务（与 version/outbox 同一事务）。"""
        row = ParsingJobRow(
            tenant_id=tenant_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            credential_id=credential_id,
            document_id=document_id,
            version_id=version_id,
            dataset_id=dataset_id,
            state=JobState.PENDING.value,
            attempt=1,
            execution_plan=execution_plan,
            effective_config_digest=effective_config_digest,
            pool=pool,
            parse_only=parse_only,
            progress={},
        )
        session.add(row)
        await session.flush()
        await self._append_event(
            session,
            row,
            event_type="job.created",
            from_state=None,
            to_state=JobState.PENDING.value,
            detail={"pool": pool, "parse_only": parse_only},
        )
        return row.id

    async def get_job(self, tenant_id: uuid.UUID, job_id: uuid.UUID) -> JobInfo:
        async with self._uow.read_only() as session:
            row = await self._get_job_row(session, tenant_id, job_id)
            return self._info(row)

    async def get_job_by_id(self, job_id: uuid.UUID) -> JobInfo | None:
        """worker 专用：不带租户过滤（任务 ID 已由上层可信来源提供）。"""
        async with self._uow.read_only() as session:
            row = (
                await session.execute(select(ParsingJobRow).where(ParsingJobRow.id == job_id))
            ).scalar_one_or_none()
            return self._info(row) if row is not None else None

    async def get_job_by_version(self, tenant_id: uuid.UUID, version_id: uuid.UUID) -> JobInfo | None:
        async with self._uow.read_only() as session:
            row = (
                await session.execute(
                    select(ParsingJobRow).where(
                        ParsingJobRow.tenant_id == tenant_id,
                        ParsingJobRow.version_id == version_id,
                    )
                )
            ).scalar_one_or_none()
            return self._info(row) if row is not None else None

    async def list_jobs(
        self,
        tenant_id: uuid.UUID,
        *,
        actor_kind: str | None,
        actor_id: uuid.UUID | None,
        state: str | None,
        dataset_id: uuid.UUID | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[JobInfo], str | None, bool]:
        conditions = [ParsingJobRow.tenant_id == tenant_id]
        if actor_kind is not None:
            conditions.append(ParsingJobRow.actor_kind == actor_kind)
        if actor_id is not None:
            conditions.append(ParsingJobRow.actor_id == actor_id)
        if state is not None:
            conditions.append(ParsingJobRow.state == state)
        if dataset_id is not None:
            conditions.append(ParsingJobRow.dataset_id == dataset_id)
        if cursor:
            data = decode_cursor(cursor)
            created = data.get("ts")
            last_id = data.get("id")
            if created and last_id:
                from datetime import datetime as dt

                conditions.append(ParsingJobRow.created_at < dt.fromisoformat(str(created)))
        async with self._uow.read_only() as session:
            rows = (
                (
                    await session.execute(
                        select(ParsingJobRow)
                        .where(*conditions)
                        .order_by(ParsingJobRow.created_at.desc(), ParsingJobRow.id)
                        .limit(limit + 1)
                    )
                )
                .scalars()
                .all()
            )
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            encode_cursor({"ts": items[-1].created_at.isoformat(), "id": str(items[-1].id)})
            if has_more and items
            else None
        )
        return [self._info(r) for r in items], next_cursor, has_more

    # ------------------------------------------------------------------
    # 租约与状态推进（worker）
    # ------------------------------------------------------------------

    async def acquire_lease(self, job_id: uuid.UUID, owner: str) -> int | None:
        """CAS 领取执行租约：PENDING/RETRY_WAIT -> ROUTING；返回 fence。"""
        now = self._clock.now()
        lease_until = now + timedelta(seconds=self._lease_seconds)
        async with self._uow.begin() as session:
            result = await session.execute(
                update(ParsingJobRow)
                .where(
                    ParsingJobRow.id == job_id,
                    ParsingJobRow.state.in_([JobState.PENDING.value, JobState.RETRY_WAIT.value]),
                )
                .values(
                    state=JobState.ROUTING.value,
                    lease_owner=owner,
                    lease_until=lease_until,
                    fence=ParsingJobRow.fence + 1,
                    stage=JobState.ROUTING.value,
                )
                .returning(ParsingJobRow.fence, ParsingJobRow.state)
            )
            row = result.one_or_none()
            if row is None:
                return None
            fence: int = row[0]
            job = (
                await session.execute(select(ParsingJobRow).where(ParsingJobRow.id == job_id))
            ).scalar_one()
            await self._append_event(
                session,
                job,
                event_type="job.state_changed",
                from_state=JobState.PENDING.value,
                to_state=JobState.ROUTING.value,
                detail={"owner": owner},
            )
            return fence

    async def heartbeat(self, job_id: uuid.UUID, fence: int) -> bool:
        now = self._clock.now()
        async with self._uow.begin() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(ParsingJobRow)
                    .where(ParsingJobRow.id == job_id, ParsingJobRow.fence == fence)
                    .values(lease_until=now + timedelta(seconds=self._lease_seconds))
                ),
            )
            return result.rowcount == 1

    async def is_cancel_requested(self, job_id: uuid.UUID) -> bool:
        async with self._uow.read_only() as session:
            row = (
                await session.execute(
                    select(ParsingJobRow.cancel_requested_at).where(ParsingJobRow.id == job_id)
                )
            ).one_or_none()
            return bool(row and row[0] is not None)

    async def transition(
        self,
        job_id: uuid.UUID,
        fence: int,
        to_state: str,
        *,
        stage: str | None = None,
        progress: dict[str, Any] | None = None,
        total_units: int | None = None,
        completed_units: int | None = None,
        coverage: float | None = None,
        error: dict[str, Any] | None = None,
        retrieval_ready: bool | None = None,
        parse_only: bool | None = None,
        event_detail: dict[str, Any] | None = None,
    ) -> bool:
        """CAS 状态推进：fence 必须匹配当前 fence；校验状态机允许的转换。

        返回 False 表示旧 fence 或状态不允许（旧 worker 不得继续提交）。
        """
        async with self._uow.begin() as session:
            row = (
                await session.execute(select(ParsingJobRow).where(ParsingJobRow.id == job_id))
            ).scalar_one_or_none()
            if row is None:
                return False
            if row.fence != fence:
                return False  # 旧 fence：不得覆盖已提交状态
            prev_state = row.state
            if prev_state != to_state:
                domain.assert_transition(prev_state, to_state)
            values: dict[str, Any] = {"state": to_state}
            if stage is not None:
                values["stage"] = stage
            if progress is not None:
                merged = dict(row.progress or {})
                merged.update(progress)
                values["progress"] = merged
            if total_units is not None:
                values["total_units"] = total_units
            if completed_units is not None:
                values["completed_units"] = completed_units
            if coverage is not None:
                values["coverage"] = coverage
            if error is not None:
                values["error"] = error
            if retrieval_ready is not None:
                values["retrieval_ready"] = retrieval_ready
            if parse_only is not None:
                values["parse_only"] = parse_only
            if domain.is_terminal(to_state):
                values["lease_owner"] = None
                values["lease_until"] = None
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(ParsingJobRow)
                    .where(
                        ParsingJobRow.id == job_id,
                        ParsingJobRow.fence == fence,
                        ParsingJobRow.state == prev_state,
                    )
                    .values(**values)
                ),
            )
            if result.rowcount != 1:
                return False
            await session.refresh(row)
            await self._append_event(
                session,
                row,
                event_type="job.state_changed",
                from_state=prev_state,
                to_state=to_state,
                detail=event_detail or {},
            )
            return True

    async def begin_cancelling(self, job_id: uuid.UUID) -> bool:
        """请求取消：非终态 -> CANCELLING。"""
        from owli_parser.core.errors import InvalidStateError

        async with self._uow.begin() as session:
            row = (
                await session.execute(select(ParsingJobRow).where(ParsingJobRow.id == job_id))
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("任务不存在")
            if domain.is_terminal(row.state):
                return False
            if row.state == JobState.CANCELLING.value:
                return True
            if row.state not in _cancellable_states():
                raise InvalidStateError("当前状态不支持取消")
            prev = row.state
            row.state = JobState.CANCELLING.value
            row.cancel_requested_at = self._clock.now()
            await self._append_event(
                session,
                row,
                event_type="job.cancel_requested",
                from_state=prev,
                to_state=JobState.CANCELLING.value,
                detail={},
            )
            return True

    async def finalize_cancelled(self, job_id: uuid.UUID, fence: int | None) -> None:
        async with self._uow.begin() as session:
            row = (
                await session.execute(select(ParsingJobRow).where(ParsingJobRow.id == job_id))
            ).scalar_one_or_none()
            if row is None:
                return
            if fence is not None and row.fence != fence:
                return
            prev = row.state
            row.state = JobState.CANCELLED.value
            row.lease_owner = None
            row.lease_until = None
            await self._append_event(
                session,
                row,
                event_type="job.state_changed",
                from_state=prev,
                to_state=JobState.CANCELLED.value,
                detail={},
            )

    async def retry_job(self, tenant_id: uuid.UUID, job_id: uuid.UUID, *, max_attempts: int) -> JobInfo:
        """FAILED/DEAD_LETTER -> RETRY_WAIT（推进 attempt）；终态不可原地重启。"""
        async with self._uow.begin() as session:
            row = await self._get_job_row(session, tenant_id, job_id)
            if row.state not in (JobState.FAILED.value, JobState.DEAD_LETTER.value):
                raise ConflictError("只有失败或死信任务可以重试")
            if row.attempt >= max_attempts:
                raise ConflictError("重试次数已达上限，请创建新版本重试")
            prev = row.state
            row.attempt += 1
            row.state = JobState.RETRY_WAIT.value
            row.error = None
            await self._append_event(
                session,
                row,
                event_type="job.retry_scheduled",
                from_state=prev,
                to_state=JobState.RETRY_WAIT.value,
                detail={"attempt": row.attempt},
            )
            return self._info(row)

    # ------------------------------------------------------------------
    # 事件与 checkpoint
    # ------------------------------------------------------------------

    async def list_events(
        self, tenant_id: uuid.UUID, job_id: uuid.UUID, *, after_seq: int, limit: int = 200
    ) -> tuple[list[JobEventInfo], int]:
        async with self._uow.read_only() as session:
            await self._get_job_row(session, tenant_id, job_id)
            rows = (
                (
                    await session.execute(
                        select(JobEventRow)
                        .where(JobEventRow.job_id == job_id, JobEventRow.seq > after_seq)
                        .order_by(JobEventRow.seq)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            latest = (
                await session.execute(
                    select(func.coalesce(func.max(JobEventRow.seq), 0)).where(JobEventRow.job_id == job_id)
                )
            ).scalar_one()
            items = [
                JobEventInfo(
                    seq=r.seq,
                    event_type=r.event_type,
                    attempt=r.attempt,
                    from_state=r.from_state,
                    to_state=r.to_state,
                    ts=r.ts,
                    detail=r.detail or {},
                )
                for r in rows
            ]
            return items, int(latest)

    async def list_steps(
        self,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        *,
        stage: str | None = None,
        limit: int = 1000,
    ) -> tuple[list[JobStepInfo], int]:
        """单位级步骤（页/时间窗）：先做租户校验（跨租户 404），再返回有界列表。"""
        await self.get_job(tenant_id, job_id)
        conditions = [JobStepRow.job_id == job_id, JobStepRow.tenant_id == tenant_id]
        if stage is not None:
            conditions.append(JobStepRow.stage == stage)
        async with self._uow.read_only() as session:
            total = (
                await session.execute(select(func.count()).select_from(JobStepRow).where(*conditions))
            ).scalar_one()
            rows = (
                (
                    await session.execute(
                        select(JobStepRow)
                        .where(*conditions)
                        .order_by(JobStepRow.stage, JobStepRow.unit_id, JobStepRow.attempt)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [self._step_info(row) for row in rows], int(total)

    async def record_step(
        self,
        session: Any,
        *,
        job_id: uuid.UUID,
        tenant_id: uuid.UUID,
        stage: str,
        unit_id: str,
        attempt: int,
        state: str,
        checkpoint_ref: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        """登记/更新单位级步骤记录（checkpoint 只引用校验通过的不可变产物）。"""
        now = self._clock.now()
        existing = (
            await session.execute(
                select(JobStepRow).where(
                    JobStepRow.job_id == job_id,
                    JobStepRow.stage == stage,
                    JobStepRow.unit_id == unit_id,
                    JobStepRow.attempt == attempt,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                JobStepRow(
                    job_id=job_id,
                    tenant_id=tenant_id,
                    stage=stage,
                    unit_id=unit_id,
                    attempt=attempt,
                    state=state,
                    checkpoint_ref=checkpoint_ref,
                    error_code=error_code,
                    started_at=now,
                    finished_at=now if state in ("succeeded", "failed") else None,
                )
            )
        else:
            existing.state = state
            if checkpoint_ref is not None:
                existing.checkpoint_ref = checkpoint_ref
            existing.error_code = error_code
            if state in ("succeeded", "failed"):
                existing.finished_at = now

    async def succeeded_units(self, job_id: uuid.UUID, stage: str, attempt: int | None = None) -> set[str]:
        async with self._uow.read_only() as session:
            conditions = [
                JobStepRow.job_id == job_id,
                JobStepRow.stage == stage,
                JobStepRow.state == "succeeded",
            ]
            if attempt is not None:
                conditions.append(JobStepRow.attempt == attempt)
            rows = (await session.execute(select(JobStepRow.unit_id).where(*conditions))).scalars().all()
            return set(rows)

    @staticmethod
    def _step_info(row: JobStepRow) -> JobStepInfo:
        return JobStepInfo(
            stage=row.stage,
            unit_id=row.unit_id,
            attempt=row.attempt,
            state=row.state,
            checkpoint_ref=row.checkpoint_ref,
            error_code=row.error_code,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _append_event(
        self,
        session: Any,
        job: ParsingJobRow,
        *,
        event_type: str,
        from_state: str | None,
        to_state: str | None,
        detail: dict[str, Any] | None,
    ) -> None:
        next_seq = (
            await session.execute(
                select(func.coalesce(func.max(JobEventRow.seq), 0)).where(JobEventRow.job_id == job.id)
            )
        ).scalar_one()
        session.add(
            JobEventRow(
                tenant_id=job.tenant_id,
                job_id=job.id,
                seq=int(next_seq) + 1,
                schema_version=EVENT_SCHEMA_VERSION,
                event_type=event_type,
                attempt=job.attempt,
                from_state=from_state,
                to_state=to_state,
                ts=self._clock.now(),
                detail=detail or {},
            )
        )

    async def _get_job_row(self, session: Any, tenant_id: uuid.UUID, job_id: uuid.UUID) -> ParsingJobRow:
        row = (
            await session.execute(
                select(ParsingJobRow).where(ParsingJobRow.id == job_id, ParsingJobRow.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("任务不存在")
        return cast(ParsingJobRow, row)

    @staticmethod
    def _info(row: ParsingJobRow) -> JobInfo:
        return JobInfo(
            id=row.id,
            tenant_id=row.tenant_id,
            actor_id=row.actor_id,
            document_id=row.document_id,
            version_id=row.version_id,
            dataset_id=row.dataset_id,
            state=row.state,
            stage=row.stage,
            attempt=row.attempt,
            pool=row.pool,
            progress=row.progress or {},
            total_units=row.total_units,
            completed_units=row.completed_units,
            coverage=row.coverage,
            error=row.error,
            parse_only=row.parse_only,
            retrieval_ready=row.retrieval_ready,
            fence=row.fence,
            effective_config_digest=row.effective_config_digest,
            execution_plan=dict(row.execution_plan or {}),
        )

    async def sweep_expired_leases(
        self, *, batch: int, on_expired: Callable[[uuid.UUID], None] | None = None
    ) -> int:
        """回收过期租约：运行阶段 + lease 过期 -> RETRY_WAIT（attempt 由 retry 预算控制）。"""
        now = self._clock.now()
        running = [
            JobState.ROUTING.value,
            JobState.PARSING.value,
            JobState.ENRICHING.value,
            JobState.NORMALIZING.value,
            JobState.CHUNKING.value,
            JobState.INDEXING.value,
            JobState.PUBLISHING.value,
        ]
        async with self._uow.begin() as session:
            rows = (
                (
                    await session.execute(
                        select(ParsingJobRow)
                        .where(
                            ParsingJobRow.state.in_(running),
                            ParsingJobRow.lease_until.is_not(None),
                            ParsingJobRow.lease_until < now,
                        )
                        .limit(batch)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                prev = row.state
                row.state = JobState.RETRY_WAIT.value
                row.lease_owner = None
                row.lease_until = None
                row.error = {"code": "lease_expired", "message": "执行租约过期，等待重新调度"}
                await self._append_event(
                    session,
                    row,
                    event_type="job.lease_expired",
                    from_state=prev,
                    to_state=JobState.RETRY_WAIT.value,
                    detail={},
                )
                if on_expired is not None:
                    on_expired(row.id)
            return len(rows)


def _cancellable_states() -> set[str]:
    from owli_parser.contracts.events import ALLOWED_JOB_TRANSITIONS

    result: set[str] = set()
    for state, targets in ALLOWED_JOB_TRANSITIONS.items():
        if JobState.CANCELLING in targets:
            result.add(state.value)
    return result
