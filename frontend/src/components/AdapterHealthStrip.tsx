/**
 * Adapter 健康条（M4-6）：展示内置/外部实例数量与失联清单。
 *
 * - 数据来自 GET /v1/adapters（health 已是读时生效值），不重复计算 TTL；
 * - 仅存在外部注册实例或非 ok 时展示，避免默认安装下的噪音。
 */
import type { AdapterInfo } from '../api/client';

export interface AdapterHealthSummary {
  status: 'ok' | 'degraded' | 'unavailable';
  total: number;
  registered: number;
  counts: Record<string, number>;
  stale: string[];
}

export function summarizeAdapterHealth(items: AdapterInfo[]): AdapterHealthSummary {
  const counts: Record<string, number> = {};
  const stale: string[] = [];
  for (const item of items) {
    const health = typeof item.health === 'string' ? item.health : 'unknown';
    counts[health] = (counts[health] ?? 0) + 1;
    if (item.stale) stale.push(item.name);
  }
  const status = counts.unavailable
    ? 'unavailable'
    : counts.degraded || counts.maintenance || counts.unknown
      ? 'degraded'
      : 'ok';
  return {
    status,
    total: items.length,
    registered: items.filter((item) => item.source === 'registered').length,
    counts,
    stale,
  };
}

export function AdapterHealthStrip({ items }: { items: AdapterInfo[] }) {
  const summary = summarizeAdapterHealth(items);
  if (summary.total === 0) return null;
  if (summary.registered === 0 && summary.status === 'ok') return null;

  const statusClass =
    summary.status === 'ok'
      ? 'text-state-ok'
      : summary.status === 'degraded'
        ? 'text-state-warn'
        : 'text-state-danger';
  const labels: Record<string, string> = {
    ready: 'ready',
    degraded: 'degraded',
    maintenance: 'maintenance',
    unavailable: 'unavailable',
    unknown: 'unknown',
  };

  return (
    <div
      data-testid="adapter-health"
      className="flex flex-wrap items-center gap-2 rounded-card border border-surface-border bg-surface-muted/40 px-2 py-1 text-xs"
    >
      <span className={`font-medium ${statusClass}`}>adapter {summary.status}</span>
      <span className="text-ink-muted">
        共 {summary.total}（外部 {summary.registered}）
      </span>
      <span className="text-ink-muted tabular-nums">
        {Object.entries(labels)
          .filter(([key]) => summary.counts[key])
          .map(([key, label]) => `${label} ${summary.counts[key]}`)
          .join(' · ')}
      </span>
      {summary.stale.length > 0 && (
        <span className="text-state-warn">失联：{summary.stale.join('、')}</span>
      )}
    </div>
  );
}
