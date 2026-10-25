/** 共享小组件：状态徽章、空态、错误态与加载骨架。 */
import type { ReactNode } from 'react';

const STATE_STYLES: Record<string, string> = {
  PENDING: 'bg-surface-muted text-ink-muted',
  ROUTING: 'bg-brand-soft text-brand',
  PARSING: 'bg-brand-soft text-brand',
  ENRICHING: 'bg-brand-soft text-brand',
  NORMALIZING: 'bg-brand-soft text-brand',
  CHUNKING: 'bg-brand-soft text-brand',
  INDEXING: 'bg-brand-soft text-brand',
  PUBLISHING: 'bg-brand-soft text-brand',
  DONE: 'bg-surface-muted text-state-ok',
  PARTIAL_DONE: 'bg-surface-muted text-state-warn',
  RETRY_WAIT: 'bg-surface-muted text-state-warn',
  FAILED: 'bg-surface-muted text-state-danger',
  DEAD_LETTER: 'bg-surface-muted text-state-danger',
  CANCELLED: 'bg-surface-muted text-ink-muted',
  SUBMITTED: 'bg-surface-muted text-state-ok',
  READY: 'bg-surface-muted text-state-ok',
  DISABLED: 'bg-surface-muted text-ink-muted',
  FETCHING: 'bg-brand-soft text-brand',
};

export function StateBadge({ state }: { state: string }) {
  const style = STATE_STYLES[state] ?? 'bg-surface-muted text-ink-muted';
  return (
    <span className={`rounded-tag px-2 py-0.5 text-xs tabular-nums ${style}`}>{state}</span>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="rounded-card border border-dashed border-surface-border p-10 text-center">
      <p className="text-sm text-ink">{title}</p>
      {hint && <p className="mt-1 text-xs text-ink-muted">{hint}</p>}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="rounded-card border border-surface-border bg-surface p-6">
      <p className="text-sm text-state-danger">{message}</p>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 rounded-btn border border-surface-border px-3 py-1.5 text-xs hover:bg-surface-muted"
        >
          重试
        </button>
      )}
    </div>
  );
}

export function SkeletonRows({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-busy="true" aria-live="polite">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="h-10 animate-pulse rounded-card bg-surface-muted" />
      ))}
    </div>
  );
}

export function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-card border border-surface-border bg-surface shadow-rest ${className}`}>
      {children}
    </div>
  );
}
