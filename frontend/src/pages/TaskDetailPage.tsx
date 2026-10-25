/**
 * 任务详情：WebSocket 事件流 + 快照轮询降级（useJobEvents）。
 * - 不按定时器伪造百分比：total_units 未知时显示不定进度。
 * - 取消/重试走权威接口；状态以事件流（或降级轮询）为准。
 */
import { useMutation, useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { ApiError, jobSteps, request, type JobStepItem } from '../api/client';
import { Card, ErrorState, SkeletonRows, StateBadge } from '../components/shared';
import { useJobEvents } from '../hooks/useJobEvents';
import { useSessionStore } from '../stores/session';

const STAGES = [
  'ROUTING',
  'PARSING',
  'ENRICHING',
  'NORMALIZING',
  'CHUNKING',
  'INDEXING',
  'PUBLISHING',
] as const;

const TERMINAL = ['DONE', 'PARTIAL_DONE', 'FAILED', 'DEAD_LETTER', 'CANCELLED'];

const MODE_LABEL: Record<string, string> = {
  connecting: '连接中…',
  live: '实时事件流',
  polling: '轮询降级',
  closed: '已结束',
};

const MODE_STYLE: Record<string, string> = {
  connecting: 'text-ink-muted',
  live: 'text-state-ok',
  polling: 'text-state-warn',
  closed: 'text-ink-muted',
};

const STEP_STATE_LABEL: Record<string, string> = {
  succeeded: '成功',
  failed: '失败',
  running: '执行中',
};

const STEP_STATE_STYLE: Record<string, string> = {
  succeeded: 'text-state-ok',
  failed: 'text-state-danger',
  running: 'text-brand',
};

/** 页级/文档级解析步骤（L-4）：成功/失败逐单位可见，超限时明确提示。 */
export function JobStepsCard({
  steps,
  total,
  limit,
  pending,
  error,
}: {
  steps: JobStepItem[];
  total: number;
  limit: number;
  pending: boolean;
  error: boolean;
}) {
  return (
    <Card>
      <div className="border-b border-surface-border px-4 py-2 text-sm font-medium">
        解析步骤（页级单位）{total > 0 ? ` · ${total}` : ''}
      </div>
      <div className="p-4 text-sm">
        {error && (
          <p role="alert" className="text-xs text-state-danger">
            步骤加载失败
          </p>
        )}
        {!error && pending && steps.length === 0 && (
          <p className="text-xs text-ink-muted">加载中…</p>
        )}
        {!error && !pending && steps.length === 0 && (
          <p className="text-xs text-ink-muted">暂无步骤：解析完成后按页/文档登记。</p>
        )}
        {steps.length > 0 && (
          <table data-testid="job-steps-table" className="w-full text-xs">
            <thead>
              <tr className="border-b border-surface-border text-left text-ink-muted">
                <th className="px-2 py-1.5">单位</th>
                <th className="px-2 py-1.5">阶段</th>
                <th className="px-2 py-1.5">attempt</th>
                <th className="px-2 py-1.5">状态</th>
                <th className="px-2 py-1.5">错误</th>
                <th className="px-2 py-1.5">完成时间</th>
              </tr>
            </thead>
            <tbody>
              {steps.map((step) => (
                <tr
                  key={`${step.stage}:${step.unit_id}:${step.attempt}`}
                  className="border-b border-surface-border last:border-b-0"
                >
                  <td className="px-2 py-1.5 font-mono">{step.unit_id}</td>
                  <td className="px-2 py-1.5">{step.stage}</td>
                  <td className="px-2 py-1.5 tabular-nums">{step.attempt}</td>
                  <td
                    className={`px-2 py-1.5 ${STEP_STATE_STYLE[step.state] ?? 'text-ink-muted'}`}
                  >
                    {STEP_STATE_LABEL[step.state] ?? step.state}
                  </td>
                  <td className="px-2 py-1.5">{step.error_code ?? '—'}</td>
                  <td className="px-2 py-1.5 text-ink-muted">
                    {step.finished_at ? new Date(step.finished_at).toLocaleTimeString() : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {total > steps.length && (
          <p className="mt-2 text-xs text-ink-muted">
            已显示 {steps.length}/{total} 条（limit={limit}），可用 stage 过滤或提高 limit。
          </p>
        )}
      </div>
    </Card>
  );
}

export function TaskDetailPage() {
  const { jobId = '' } = useParams();
  const tenantId = useSessionStore((s) => s.currentTenantId);
  const csrfToken = useSessionStore((s) => s.csrfToken);
  const { mode, job, events } = useJobEvents(jobId || null);

  const steps = useQuery({
    queryKey: ['job-steps', tenantId, jobId],
    enabled: Boolean(tenantId && jobId),
    queryFn: () => jobSteps({ jobId, tenantId: tenantId ?? '' }),
    refetchInterval: job && TERMINAL.includes(job.state) ? false : 5000,
  });

  const cancel = useMutation({
    mutationFn: () =>
      request(`/parsing/jobs/${jobId}/cancel`, { method: 'POST', tenantId, csrfToken }),
  });
  const retry = useMutation({
    mutationFn: () =>
      request(`/parsing/jobs/${jobId}/retry`, { method: 'POST', tenantId, csrfToken }),
  });

  if (!job && mode === 'connecting') return <SkeletonRows />;
  if (!job) {
    return <ErrorState message="任务不存在或暂时不可读取" />;
  }

  const isTerminal = TERMINAL.includes(job.state);
  const succeeded = job.state === 'DONE' || job.state === 'PARTIAL_DONE';
  const failed = job.state === 'FAILED' || job.state === 'DEAD_LETTER';
  // 终态任务的 stage 可能已推进到 DONE：成功完成视为全部阶段已到达，不靠猜测
  const currentStageIndex = succeeded
    ? STAGES.length - 1
    : job.stage
      ? STAGES.indexOf(job.stage as (typeof STAGES)[number])
      : -1;

  return (
    <div className="mx-auto max-w-5xl space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/tasks" className="text-sm text-brand hover:underline">
          ← 任务中心
        </Link>
        <h1 className="text-lg font-semibold">任务 {job.id.slice(0, 8)}</h1>
        <StateBadge state={job.state} />
        <span
          className={`text-xs ${MODE_STYLE[mode] ?? 'text-ink-muted'}`}
          role="status"
          aria-live="polite"
        >
          {MODE_LABEL[mode] ?? mode}
        </span>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card>
          <div className="border-b border-surface-border px-4 py-2 text-sm font-medium">
            任务快照
          </div>
          <dl className="grid grid-cols-2 gap-2 p-4 text-sm">
            <dt className="text-xs text-ink-muted">文档</dt>
            <dd>
              <Link
                className="text-brand hover:underline"
                to={`/documents/${job.document_id}?version=${job.version_id}`}
              >
                {job.document_id.slice(0, 8)}
              </Link>
            </dd>
            <dt className="text-xs text-ink-muted">attempt</dt>
            <dd className="tabular-nums">{job.attempt}</dd>
            <dt className="text-xs text-ink-muted">阶段</dt>
            <dd>{job.stage ?? '—'}</dd>
            <dt className="text-xs text-ink-muted">进度</dt>
            <dd className="tabular-nums">
              {job.total_units !== null
                ? `${job.completed_units}/${job.total_units}`
                : '不定进度'}
            </dd>
            <dt className="text-xs text-ink-muted">覆盖率</dt>
            <dd className="tabular-nums">
              {job.coverage !== null ? `${(job.coverage * 100).toFixed(0)}%` : '—'}
            </dd>
            <dt className="text-xs text-ink-muted">可检索</dt>
            <dd>{job.retrieval_ready ? '是（索引已确认）' : '否（未启用或索引未确认）'}</dd>
          </dl>
          {job.error && (
            <div role="alert" className="border-t border-surface-border px-4 py-2 text-xs text-state-danger">
              {String(job.error.code ?? 'error')}：{String(job.error.message ?? '')}
            </div>
          )}
          <div className="flex gap-2 border-t border-surface-border px-4 py-3">
            {!isTerminal && (
              <button
                type="button"
                onClick={() => {
                  if (window.confirm('确认取消该任务？已发布的结果不受影响。')) {
                    cancel.mutate();
                  }
                }}
                disabled={cancel.isPending}
                className="rounded-btn border border-surface-border px-3 py-1.5 text-xs text-state-danger hover:bg-surface-muted disabled:opacity-50"
              >
                取消任务
              </button>
            )}
            {failed && (
              <button
                type="button"
                onClick={() => retry.mutate()}
                disabled={retry.isPending}
                className="rounded-btn bg-brand px-3 py-1.5 text-xs text-white disabled:opacity-50"
              >
                重试（推进 attempt）
              </button>
            )}
          </div>
          {(cancel.isError || retry.isError) && (
            <p role="alert" className="px-4 pb-3 text-xs text-state-danger">
              {(cancel.error ?? retry.error) instanceof ApiError
                ? ((cancel.error ?? retry.error) as ApiError).message
                : '操作失败'}
            </p>
          )}
        </Card>

        <Card>
          <div className="border-b border-surface-border px-4 py-2 text-sm font-medium">
            阶段
          </div>
          <ol className="space-y-1 p-4 text-sm">
            {STAGES.map((stage, index) => {
              const reached = currentStageIndex >= 0 && index <= currentStageIndex;
              const isCurrent = job.stage === stage && !isTerminal;
              return (
                <li key={stage} className="flex items-center gap-2">
                  <span
                    aria-hidden="true"
                    className={`inline-block h-2 w-2 rounded-full ${
                      failed && isCurrent
                        ? 'bg-state-danger'
                        : reached
                          ? 'bg-state-ok'
                          : 'bg-surface-border'
                    }`}
                  />
                  <span className={reached ? 'text-ink' : 'text-ink-muted'}>{stage}</span>
                  {isCurrent && (
                    <span className="text-xs text-ink-muted">
                      {failed ? '失败' : '执行中'}
                    </span>
                  )}
                </li>
              );
            })}
          </ol>
        </Card>

        <Card>
          <div className="border-b border-surface-border px-4 py-2 text-sm font-medium">
            事件时间线（{events.length}）
          </div>
          <ul className="max-h-[28rem] divide-y divide-surface-border overflow-y-auto text-xs">
            {events.map((event) => (
              <li key={event.seq} className="px-4 py-2">
                <div className="flex items-center gap-2">
                  <span className="tabular-nums text-ink-muted">#{event.seq}</span>
                  <span className="text-ink">{event.type}</span>
                  {event.attempt !== null && (
                    <span className="text-ink-muted">attempt {event.attempt}</span>
                  )}
                </div>
                <div className="mt-0.5 text-ink-muted">
                  {event.from_state ?? '—'} → {event.to_state ?? '—'} ·{' '}
                  <time dateTime={event.ts}>{new Date(event.ts).toLocaleTimeString()}</time>
                </div>
              </li>
            ))}
            {events.length === 0 && (
              <li className="px-4 py-3 text-ink-muted">等待事件…（快照已加载）</li>
            )}
          </ul>
        </Card>
      </div>

      <JobStepsCard
        steps={steps.data?.items ?? []}
        total={steps.data?.total ?? 0}
        limit={steps.data?.limit ?? 1000}
        pending={steps.isPending}
        error={steps.isError}
      />
    </div>
  );
}
