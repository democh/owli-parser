"""运行时监控指标（Prometheus 文本）：任务积压、租约心跳、outbox 滞后。

- 由 /metrics 端点拉取时从 PG 现算（拉取频率低，不做常驻采样）；
- 只输出聚合数值与有界标签，不含任务 ID、租户 ID 等可关联信息；
- 时钟归一（ensure_utc）以兼容 PG/sqlite 的 naive/aware 差异。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from owli_parser.core.clock import ensure_utc
from owli_parser.infrastructure.crosscut_models import OutboxEventRow
from owli_parser.modules.jobs.models import ParsingJobRow

# 非终态任务（积压口径）：终态见 contracts.events.JobState
_TERMINAL_STATES = ("DONE", "PARTIAL_DONE", "FAILED", "DEAD_LETTER", "CANCELLED")


async def collect_runtime_metrics(container: Any) -> str:
    """拉取时计算的运行时 gauge（未就绪时返回空串，不影响既有计数指标）。"""
    now = container.clock.now()
    lines: list[str] = []
    async with container.uow.read_only() as session:
        pending_total = (
            await session.execute(
                select(func.count())
                .select_from(ParsingJobRow)
                .where(ParsingJobRow.state.not_in(_TERMINAL_STATES))
            )
        ).scalar_one()
        oldest_created = (
            await session.execute(
                select(func.min(ParsingJobRow.created_at)).where(ParsingJobRow.state.not_in(_TERMINAL_STATES))
            )
        ).scalar_one_or_none()
        active_leases = (
            await session.execute(
                select(func.count())
                .select_from(ParsingJobRow)
                .where(ParsingJobRow.lease_until.is_not(None), ParsingJobRow.lease_until > now)
            )
        ).scalar_one()
        outbox_pending = (
            await session.execute(
                select(func.count())
                .select_from(OutboxEventRow)
                .where(OutboxEventRow.delivery_state == "pending")
            )
        ).scalar_one()
        outbox_oldest = (
            await session.execute(
                select(func.min(OutboxEventRow.available_at)).where(
                    OutboxEventRow.delivery_state == "pending"
                )
            )
        ).scalar_one_or_none()

    lines.append(f"owli_jobs_pending_total {int(pending_total)}")
    age = (now - ensure_utc(oldest_created)).total_seconds() if oldest_created else 0.0
    lines.append(f"owli_jobs_oldest_pending_seconds {max(age, 0.0):.3f}")
    lines.append(f"owli_active_leases_total {int(active_leases)}")
    lines.append(f"owli_outbox_pending_total {int(outbox_pending)}")
    lag = (now - ensure_utc(outbox_oldest)).total_seconds() if outbox_oldest else 0.0
    lines.append(f"owli_outbox_oldest_pending_seconds {max(lag, 0.0):.3f}")
    return "\n".join(lines) + "\n"
