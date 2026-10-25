/**
 * 复核队列：低置信 / 缺失单位的人工审核入口（文档 M2 复核可追溯）。
 *
 * - 处置为 CAS：仅 open 可处置，重复处置服务端返回 409（前端提示刷新，
 *   不静默覆盖他人结论）；
 * - 处置动作与后端白名单一致（accept/fix/rerun_page/switch_adapter/dismiss）；
 * - ReviewBoard 为纯展示组件（数据层与展示分离，便于单测覆盖各状态）。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Link } from 'react-router-dom';
import {
  ApiError,
  REVIEW_ACTIONS,
  resolveReviewItem,
  reviewQueue,
  type ReviewItem,
} from '../api/client';
import { Card, EmptyState, ErrorState, SkeletonRows, StateBadge } from '../components/shared';
import { useSessionStore } from '../stores/session';

interface BoardProps {
  items: ReviewItem[];
  status: string;
  onStatusChange: (status: string) => void;
  onResolve: (itemId: string, action: string, note: string | null) => void;
  pending: boolean;
  listError: Error | null;
  resolveState: { itemId: string; kind: 'ok' | 'error'; message: string } | null;
  /** URL 驱动的过滤（工作台版本徽标等入口携带）。 */
  filter?: { versionId: string | null; reason: string | null } | null;
  onFilterChange?: (next: { versionId: string | null; reason: string | null }) => void;
}

const REASON_OPTIONS = [
  { value: '', label: '全部原因' },
  { value: 'low_confidence', label: '低置信（low_confidence）' },
  { value: 'missing_unit', label: '缺失单位（missing_unit）' },
] as const;

function shortId(value: string): string {
  return value.length > 8 ? `${value.slice(0, 8)}…` : value;
}

export function ReviewBoard({
  items,
  status,
  onStatusChange,
  onResolve,
  pending,
  listError,
  resolveState,
  filter,
  onFilterChange,
}: BoardProps) {
  const [activeId, setActiveId] = useState<string | null>(null);
  const [action, setAction] = useState<string>(REVIEW_ACTIONS[0].value);
  const [note, setNote] = useState('');

  return (
    <div className="mx-auto max-w-5xl space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">复核队列</h1>
          <p className="mt-1 text-xs text-ink-muted">
            低置信或缺失单位的人工审核；处置为 CAS（仅 open 可处置，重复处置返回 409，
            不覆盖他人结论）。
          </p>
        </div>
        <div className="flex rounded-tag border border-surface-border text-xs">
          {(
            [
              ['open', '待处置'],
              ['resolved', '已处置'],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              onClick={() => onStatusChange(value)}
              className={`px-3 py-1.5 ${status === value ? 'bg-brand-soft text-brand' : 'text-ink-muted'}`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {(filter?.versionId || filter?.reason) && (
        <div
          data-testid="review-filter"
          className="flex flex-wrap items-center gap-2 rounded-card border border-surface-border bg-surface-muted px-3 py-2 text-xs"
        >
          <span className="text-ink-muted">过滤：</span>
          {filter?.versionId && <span className="rounded-tag bg-surface px-2 py-0.5">版本 {filter.versionId.slice(0, 8)}…</span>}
          {filter?.reason && <span className="rounded-tag bg-surface px-2 py-0.5">原因 {filter.reason}</span>}
          <select
            aria-label="按原因过滤"
            value={filter?.reason ?? ''}
            onChange={(event) =>
              onFilterChange?.({
                versionId: filter?.versionId ?? null,
                reason: event.target.value || null,
              })
            }
            className="rounded-tag border border-surface-border bg-surface px-2 py-0.5"
          >
            {REASON_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => onFilterChange?.({ versionId: null, reason: null })}
            className="rounded-tag border border-surface-border px-2 py-0.5 hover:bg-surface"
          >
            清除过滤
          </button>
        </div>
      )}

      {listError && <ErrorState message={listError.message || '复核队列加载失败'} />}

      {resolveState && (
        <p
          role={resolveState.kind === 'error' ? 'alert' : 'status'}
          data-testid="resolve-state"
          className={`text-xs ${resolveState.kind === 'error' ? 'text-state-danger' : 'text-state-ok'}`}
        >
          {resolveState.message}
        </p>
      )}

      {items.length === 0 && !listError ? (
        <EmptyState
          title={status === 'open' ? '没有待处置的复核项' : '暂无已处置记录'}
          hint="低置信块会随解析任务进入此处，需人工审核后才建议参与检索。"
        />
      ) : (
        <ul className="space-y-3">
          {items.map((item) => (
            <li key={item.id}>
              <Card className="p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2 text-sm">
                    <StateBadge state={item.status === 'open' ? 'PENDING' : 'DONE'} />
                    <span className="font-medium">{item.reason}</span>
                  </div>
                  <span className="text-xs text-ink-muted tabular-nums">
                    {new Date(item.created_at).toLocaleString()}
                  </span>
                </div>
                <div className="mt-2 flex flex-wrap gap-3 text-xs text-ink-muted">
                  <span>block {item.block_id}</span>
                  <Link
                    to={item.job_id ? `/tasks/${item.job_id}` : '/tasks'}
                    className="text-brand hover:underline"
                  >
                    任务 {item.job_id ? shortId(item.job_id) : '—'}
                  </Link>
                  {item.document_id && (
                    <Link
                      to={`/documents/${item.document_id}?version_id=${item.version_id}&block_id=${encodeURIComponent(item.block_id)}`}
                      className="text-brand hover:underline"
                    >
                      打开工作台定位 ↗
                    </Link>
                  )}
                  <span>版本 {shortId(item.version_id)}</span>
                  {item.reviewer_id && <span>处置人 {shortId(item.reviewer_id)}</span>}
                </div>
                {item.resolution && (
                  <p className="mt-2 rounded-btn bg-surface-muted px-2 py-1 text-xs text-ink-muted">
                    处置：{String(item.resolution.action ?? '—')}
                    {item.resolution.note ? ` · ${String(item.resolution.note)}` : ''}
                    {item.resolution.resolved_at
                      ? ` · ${new Date(String(item.resolution.resolved_at)).toLocaleString()}`
                      : ''}
                  </p>
                )}
                {item.status === 'open' && (
                  <div className="mt-3">
                    {activeId === item.id ? (
                      <div className="space-y-2">
                        <div className="flex flex-wrap gap-2">
                          <select
                            aria-label="处置动作"
                            value={action}
                            onChange={(event) => setAction(event.target.value)}
                            className="rounded-btn border border-surface-border bg-surface px-2 py-1 text-xs"
                          >
                            {REVIEW_ACTIONS.map((option) => (
                              <option key={option.value} value={option.value}>
                                {option.label}
                              </option>
                            ))}
                          </select>
                          <input
                            aria-label="处置备注"
                            value={note}
                            onChange={(event) => setNote(event.target.value)}
                            placeholder="备注（可选，≤512 字）"
                            maxLength={512}
                            className="min-w-48 flex-1 rounded-btn border border-surface-border bg-surface px-2 py-1 text-xs"
                          />
                        </div>
                        <div className="flex gap-2">
                          <button
                            type="button"
                            disabled={pending}
                            onClick={() => {
                              onResolve(item.id, action, note.trim() || null);
                              setActiveId(null);
                              setNote('');
                            }}
                            className="rounded-btn bg-brand px-3 py-1 text-xs text-white disabled:opacity-50"
                          >
                            {pending ? '处置中…' : '确认处置'}
                          </button>
                          <button
                            type="button"
                            onClick={() => setActiveId(null)}
                            className="rounded-btn border border-surface-border px-3 py-1 text-xs"
                          >
                            取消
                          </button>
                        </div>
                      </div>
                    ) : (
                      <button
                        type="button"
                        onClick={() => {
                          setActiveId(item.id);
                          setAction(REVIEW_ACTIONS[0].value);
                          setNote('');
                        }}
                        className="rounded-btn border border-surface-border px-3 py-1 text-xs hover:bg-surface-muted"
                      >
                        处置
                      </button>
                    )}
                  </div>
                )}
              </Card>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function ReviewPage() {
  const tenantId = useSessionStore((s) => s.currentTenantId);
  const csrfToken = useSessionStore((s) => s.csrfToken);
  const queryClient = useQueryClient();
  const [status, setStatus] = useState('open');
  const [resolveState, setResolveState] = useState<BoardProps['resolveState']>(null);
  // 过滤由 URL 驱动（工作台版本徽标 → /review?version_id=…）
  const [searchParams, setSearchParams] = useSearchParams();
  const filter = {
    versionId: searchParams.get('version_id'),
    reason: searchParams.get('reason'),
  };

  const queue = useQuery({
    queryKey: ['review-queue', tenantId, status, filter.versionId, filter.reason],
    enabled: Boolean(tenantId),
    queryFn: () =>
      reviewQueue({
        status,
        tenantId: tenantId ?? '',
        versionId: filter.versionId,
        reason: filter.reason,
      }),
  });

  const resolve = useMutation({
    mutationFn: (payload: { itemId: string; action: string; note: string | null }) =>
      resolveReviewItem({ ...payload, tenantId: tenantId ?? '', csrfToken }),
    onSuccess: (_data, payload) => {
      setResolveState({ itemId: payload.itemId, kind: 'ok', message: '已处置。' });
      void queryClient.invalidateQueries({ queryKey: ['review-queue', tenantId] });
    },
    onError: (err, payload) => {
      const message =
        err instanceof ApiError && err.status === 409
          ? '该复核项已被处置（409），已刷新队列。'
          : err instanceof ApiError
            ? err.message
            : '处置失败';
      setResolveState({ itemId: payload.itemId, kind: 'error', message });
      if (err instanceof ApiError && err.status === 409) {
        void queryClient.invalidateQueries({ queryKey: ['review-queue', tenantId] });
      }
    },
  });

  if (queue.isPending) return <SkeletonRows />;

  return (
    <ReviewBoard
      items={queue.data?.items ?? []}
      status={status}
      onStatusChange={(next) => {
        setStatus(next);
        setResolveState(null);
      }}
      onResolve={(itemId, action, note) => resolve.mutate({ itemId, action, note })}
      pending={resolve.isPending}
      listError={queue.isError ? (queue.error as Error) : null}
      resolveState={resolveState}
      filter={filter}
      onFilterChange={(next) => {
        const params = new URLSearchParams();
        if (next.versionId) params.set('version_id', next.versionId);
        if (next.reason) params.set('reason', next.reason);
        setSearchParams(params);
      }}
    />
  );
}
