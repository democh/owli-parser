import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { ApiError, request, type JobItem, type ListPage } from '../api/client';
import { Card, EmptyState, ErrorState, SkeletonRows, StateBadge } from '../components/shared';
import { useSessionStore } from '../stores/session';

/** 任务中心：快照轮询（有界分页）；取消/重试按权限暴露。 */
export function TasksPage() {
  const tenantId = useSessionStore((s) => s.currentTenantId);
  const csrfToken = useSessionStore((s) => s.csrfToken);
  const queryClient = useQueryClient();

  const jobs = useQuery({
    queryKey: ['jobs', tenantId],
    enabled: Boolean(tenantId),
    queryFn: () => request<ListPage<JobItem>>('/parsing/jobs?limit=100', { tenantId }),
    refetchInterval: (query) => {
      const items = query.state.data?.items ?? [];
      const inflight = items.some(
        (j) => !['DONE', 'PARTIAL_DONE', 'FAILED', 'DEAD_LETTER', 'CANCELLED'].includes(j.state),
      );
      return inflight ? 3000 : false;
    },
  });

  const cancel = useMutation({
    mutationFn: (jobId: string) =>
      request<JobItem>(`/parsing/jobs/${jobId}/cancel`, { method: 'POST', tenantId, csrfToken }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['jobs', tenantId] }),
  });

  const retry = useMutation({
    mutationFn: (jobId: string) =>
      request<JobItem>(`/parsing/jobs/${jobId}/retry`, { method: 'POST', tenantId, csrfToken }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['jobs', tenantId] }),
  });

  if (jobs.isPending) return <SkeletonRows />;
  if (jobs.isError) {
    return (
      <ErrorState
        message={jobs.error instanceof ApiError ? jobs.error.message : '加载失败'}
        onRetry={() => void jobs.refetch()}
      />
    );
  }
  const items = jobs.data?.items ?? [];

  return (
    <div className="mx-auto max-w-6xl">
      <h1 className="mb-4 text-lg font-semibold">任务中心</h1>
      {items.length === 0 ? (
        <EmptyState title="暂无解析任务" hint="在文档中心上传或导入后会显示在这里" />
      ) : (
        <Card>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-surface-border text-left text-xs text-ink-muted">
                <th className="px-4 py-2 font-medium">任务</th>
                <th className="px-4 py-2 font-medium">状态</th>
                <th className="px-4 py-2 font-medium">阶段</th>
                <th className="px-4 py-2 font-medium">进度</th>
                <th className="px-4 py-2 font-medium">覆盖率</th>
                <th className="px-4 py-2 font-medium">错误</th>
                <th className="px-4 py-2 font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((job) => {
                const terminal = ['DONE', 'PARTIAL_DONE', 'FAILED', 'DEAD_LETTER', 'CANCELLED'].includes(
                  job.state,
                );
                return (
                  <tr key={job.id} className="border-b border-surface-border last:border-0">
                    <td className="px-4 py-2 font-mono text-xs">
                      <Link className="text-brand hover:underline" to={`/tasks/${job.id}`}>
                        {job.id.slice(0, 8)}
                      </Link>
                    </td>
                    <td className="px-4 py-2">
                      <StateBadge state={job.state} />
                    </td>
                    <td className="px-4 py-2 text-ink-muted">{job.stage ?? '—'}</td>
                    <td className="px-4 py-2 tabular-nums text-ink-muted">
                      {job.total_units !== null
                        ? `${job.completed_units}/${job.total_units}`
                        : '不定进度'}
                    </td>
                    <td className="px-4 py-2 tabular-nums text-ink-muted">
                      {job.coverage !== null ? `${(job.coverage * 100).toFixed(0)}%` : '—'}
                    </td>
                    <td className="px-4 py-2 text-xs text-state-danger">
                      {job.error?.code ? `${job.error.code}` : '—'}
                    </td>
                    <td className="px-4 py-2">
                      {!terminal && (
                        <button
                          type="button"
                          onClick={() => cancel.mutate(job.id)}
                          disabled={cancel.isPending}
                          className="text-xs text-state-danger hover:underline"
                        >
                          取消
                        </button>
                      )}
                      {(job.state === 'FAILED' || job.state === 'DEAD_LETTER') && (
                        <button
                          type="button"
                          onClick={() => retry.mutate(job.id)}
                          disabled={retry.isPending}
                          className="text-xs text-brand hover:underline"
                        >
                          重试
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Card>
      )}
      {(cancel.isError || retry.isError) && (
        <p role="alert" className="mt-3 text-sm text-state-danger">
          操作失败：{(cancel.error ?? retry.error) instanceof ApiError
            ? ((cancel.error ?? retry.error) as ApiError).message
            : '请稍后重试'}
        </p>
      )}
    </div>
  );
}
