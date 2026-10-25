/**
 * 质量回归：黄金集运行历史与指标对比（文档 §9 回归体系 / GET quality/regression-runs）。
 *
 * - 展示仅指标与判定（不含被检索内容）；
 * - 对比模式：任选两次运行并排对比 aggregate 指标（delta 正数=提升，
 *   指标缺失的一侧显示 —，不伪造 0 或 1）。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  ApiError,
  regressionRun,
  regressionRuns,
  triggerRegressionRun,
  type RegressionCase,
  type RegressionRun,
  type RegressionRunDetail,
} from '../api/client';
import { Card, EmptyState, ErrorState, SkeletonRows, StateBadge } from '../components/shared';
import { TrendChart, type TrendPoint } from '../components/TrendChart';
import { csvField, downloadText, timestampedName } from '../lib/download';
import { useSessionStore } from '../stores/session';

export interface MetricDelta {
  metric: string;
  baseline: number | null;
  candidate: number | null;
  delta: number | null; // 仅两侧都有时计算
}

/** 两次运行的指标差异（纯函数）：按指标名排序，仅单侧存在的记为 null。 */
export function diffRegressionMetrics(
  baseline: Record<string, number>,
  candidate: Record<string, number>,
): MetricDelta[] {
  const keys = [...new Set([...Object.keys(baseline), ...Object.keys(candidate)])].sort();
  return keys.map((metric) => {
    const base = baseline[metric];
    const cand = candidate[metric];
    const hasBoth = typeof base === 'number' && typeof cand === 'number';
    return {
      metric,
      baseline: typeof base === 'number' ? base : null,
      candidate: typeof cand === 'number' ? cand : null,
      delta: hasBoth ? cand - base : null,
    };
  });
}

/** 指标 diff CSV（归档）：单侧缺失值留空，delta 仅两侧都有时输出。 */
export function metricsDiffToCsv(deltas: MetricDelta[]): string {
  const lines = ['metric,baseline,candidate,delta'];
  for (const row of deltas) {
    lines.push(
      [
        csvField(row.metric),
        row.baseline !== null ? row.baseline.toFixed(6) : '',
        row.candidate !== null ? row.candidate.toFixed(6) : '',
        row.delta !== null ? row.delta.toFixed(6) : '',
      ].join(','),
    );
  }
  return `${lines.join('\n')}\n`;
}

function short(value: string | null | undefined, length = 8): string {
  if (!value) return '—';
  return value.length > length ? `${value.slice(0, length)}…` : value;
}

function runLabel(run: RegressionRun): string {
  return `${new Date(run.created_at).toLocaleString()} · ${run.adapter_ref} · ${run.state}`;
}

/** 派生可选指标名（run 的 aggregate 键并集，排序稳定）。 */
export function listMetrics(runs: RegressionRun[]): string[] {
  return [...new Set(runs.flatMap((run) => Object.keys(run.aggregate)))].sort();
}

/** 派生某指标的时间序列（仅含该指标的 run，按时间升序）；failed 点单独标记。 */
export function deriveMetricSeries(runs: RegressionRun[], metric: string): TrendPoint[] {
  return runs
    .filter((run) => typeof run.aggregate[metric] === 'number')
    .slice()
    .sort((a, b) => a.created_at.localeCompare(b.created_at))
    .map((run) => ({
      runId: run.id,
      label: `${new Date(run.created_at).toLocaleDateString()} ${run.run_id.slice(0, 6)}`,
      value: run.aggregate[metric] ?? 0,
      failed: run.state !== 'passed',
    }));
}

/** 从运行历史派生黄金集选项（id → 展示名；名缺失回退短 id）。 */
export function deriveGoldenSets(runs: RegressionRun[]): Array<{ id: string; label: string }> {
  const seen = new Map<string, string>();
  for (const run of runs) {
    if (!seen.has(run.golden_set_id)) {
      seen.set(run.golden_set_id, run.golden_set_name ?? `${run.golden_set_id.slice(0, 8)}…`);
    }
  }
  return [...seen.entries()]
    .map(([id, label]) => ({ id, label }))
    .sort((a, b) => a.label.localeCompare(b.label));
}

/**
 * 逐案明细：解析回归展示版本对，检索回归展示 query；指标为宏平均的原始输入。
 * 仅版本/查询文本与指标，不包含任何被检索正文。
 */
/** 逐案明细 CSV（归档/离线归因）：groups/metrics 以 JSON 文本落列，缺失留空。 */
export function casesToCsv(cases: RegressionCase[]): string {
  const lines = ['case,query,reference_version_id,hypothesis_version_id,groups,metrics'];
  cases.forEach((item, index) => {
    lines.push(
      [
        String(typeof item.case === 'number' ? item.case + 1 : index + 1),
        csvField(item.query ?? ''),
        csvField(item.reference_version_id ?? ''),
        csvField(item.hypothesis_version_id ?? ''),
        csvField(JSON.stringify(item.groups ?? {})),
        csvField(JSON.stringify(item.metrics ?? {})),
      ].join(','),
    );
  });
  return `${lines.join('\n')}\n`;
}

export function RegressionCases({
  cases,
  exportName,
}: {
  cases: RegressionCase[];
  /** 文件名前缀（通常为 run_id）；缺省用 regression-cases。 */
  exportName?: string;
}) {
  if (cases.length === 0) {
    return (
      <p className="text-xs text-ink-muted">该运行未记录逐案明细（可能来自旧版 CLI 记录）。</p>
    );
  }
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-ink-muted">
        <span>逐案明细（{cases.length} 例；仅版本与指标，不含被检索正文）</span>
        <button
          type="button"
          onClick={() => {
            downloadText(
              timestampedName(`regression-cases-${exportName ?? 'run'}`, 'csv'),
              casesToCsv(cases),
              'text/csv;charset=utf-8',
            );
          }}
          className="rounded-tag border border-surface-border px-2 py-0.5 hover:bg-surface-muted"
        >
          导出 cases CSV
        </button>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs" data-testid="regression-cases">
          <thead>
            <tr className="border-b border-surface-border text-left text-ink-muted">
              <th className="px-2 py-1">#</th>
              <th className="px-2 py-1">用例</th>
              <th className="px-2 py-1">分组</th>
              <th className="px-2 py-1">指标</th>
            </tr>
          </thead>
          <tbody>
            {cases.map((item, index) => (
              <tr
                key={index}
                className="border-b border-surface-border align-top last:border-b-0"
              >
                <td className="px-2 py-1 tabular-nums text-ink-muted">
                  {typeof item.case === 'number' ? item.case + 1 : index + 1}
                </td>
                <td className="px-2 py-1">
                  {item.query ? (
                    <span className="whitespace-pre-wrap">{item.query}</span>
                  ) : item.reference_version_id || item.hypothesis_version_id ? (
                    <span className="font-mono">
                      <span title={item.reference_version_id}>
                        {short(item.reference_version_id)}
                      </span>
                      {' → '}
                      <span title={item.hypothesis_version_id}>
                        {short(item.hypothesis_version_id)}
                      </span>
                    </span>
                  ) : (
                    '—'
                  )}
                </td>
                <td className="px-2 py-1">
                  {item.groups && Object.keys(item.groups).length > 0 ? (
                    <span className="flex flex-wrap gap-1">
                      {Object.entries(item.groups)
                        .sort(([a], [b]) => a.localeCompare(b))
                        .map(([key, value]) => (
                          <span key={key} className="rounded-tag bg-surface-muted px-1.5 py-0.5">
                            {key}: {value}
                          </span>
                        ))}
                    </span>
                  ) : (
                    '—'
                  )}
                </td>
                <td className="px-2 py-1">
                  {item.metrics && Object.keys(item.metrics).length > 0 ? (
                    <span className="flex flex-wrap gap-1">
                      {Object.entries(item.metrics)
                        .sort(([a], [b]) => a.localeCompare(b))
                        .map(([metric, value]) => (
                          <span
                            key={metric}
                            className="rounded-tag bg-surface-muted px-1.5 py-0.5 tabular-nums"
                          >
                            {metric} {typeof value === 'number' ? value.toFixed(4) : String(value)}
                          </span>
                        ))}
                    </span>
                  ) : (
                    '—'
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function QualityBoard({
  runs,
  listError,
  goldenSets = [],
  activeSetId = null,
  onSetChange,
  onRun,
  running = false,
  runState = null,
  details,
  detailLoadingId = null,
  detailError = null,
  onLoadDetail,
}: {
  runs: RegressionRun[];
  listError: Error | null;
  /** 可选：数据层已按集过滤时传入派生选项以保持可见。 */
  goldenSets?: Array<{ id: string; label: string }>;
  activeSetId?: string | null;
  onSetChange?: (setId: string | null) => void;
  /** 触发同步回归（需选择具体黄金集；tenant admin）。 */
  onRun?: (setId: string) => void;
  running?: boolean;
  runState?: { kind: 'ok' | 'error'; message: string } | null;
  /** 明细缓存（key=run.id）：展开后由 onLoadDetail 拉取并回填。 */
  details?: Record<string, RegressionRunDetail>;
  detailLoadingId?: string | null;
  detailError?: { runId: string; message: string } | null;
  onLoadDetail?: (runId: string) => void;
}) {
  const [baselineId, setBaselineId] = useState<string>('');
  const [candidateId, setCandidateId] = useState<string>('');
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const metrics = listMetrics(runs);
  const [trendMetric, setTrendMetric] = useState<string>(() => metrics[0] ?? '');
  const effectiveMetric = metrics.includes(trendMetric) ? trendMetric : (metrics[0] ?? '');
  const trendPoints = effectiveMetric ? deriveMetricSeries(runs, effectiveMetric) : [];

  const baseline = runs.find((run) => run.id === baselineId) ?? null;
  const candidate = runs.find((run) => run.id === candidateId) ?? null;
  const deltas = baseline && candidate ? diffRegressionMetrics(baseline.aggregate, candidate.aggregate) : null;
  // 展开行的明细（单一展开位）：用于表格渲染时的类型收窄
  const expandedDetail = expandedId ? details?.[expandedId] : undefined;

  const toggleDetail = (runId: string) => {
    if (expandedId === runId) {
      setExpandedId(null);
      return;
    }
    setExpandedId(runId);
    onLoadDetail?.(runId);
  };

  return (
    <div className="mx-auto max-w-5xl space-y-4">
      <div>
        <h1 className="text-lg font-semibold">质量回归</h1>
        <p className="mt-1 text-xs text-ink-muted">
          黄金集运行历史（`owli eval-retrieval` 结果落库）；仅指标与判定，不含被检索内容。
        </p>
      </div>

      {onSetChange && goldenSets.length >= 1 && (
        <label className="flex items-center gap-2 text-xs text-ink-muted">
          黄金集：
          <select
            aria-label="按黄金集过滤"
            value={activeSetId ?? ''}
            onChange={(event) => onSetChange(event.target.value || null)}
            className="rounded-btn border border-surface-border bg-surface px-2 py-1"
          >
            <option value="">全部黄金集</option>
            {goldenSets.map((set) => (
              <option key={set.id} value={set.id}>
                {set.label}
              </option>
            ))}
          </select>
          {onRun && (
            <button
              type="button"
              disabled={!activeSetId || running}
              onClick={() => {
                if (activeSetId) onRun(activeSetId);
              }}
              title={activeSetId ? '同步执行所选黄金集回归' : '先选择具体黄金集'}
              className="rounded-btn bg-brand px-3 py-1 text-xs text-white disabled:opacity-50"
            >
              {running ? '运行中…' : '运行回归'}
            </button>
          )}
        </label>
      )}

      {runState && (
        <p
          role={runState.kind === 'error' ? 'alert' : 'status'}
          data-testid="run-state"
          className={`px-1 text-xs ${runState.kind === 'error' ? 'text-state-danger' : 'text-state-ok'}`}
        >
          {runState.message}
        </p>
      )}

      {listError && <ErrorState message={listError.message || '回归记录加载失败'} />}

      {runs.length === 0 && !listError ? (
        <EmptyState
          title="暂无回归运行"
          hint="运行 `owli eval-retrieval --file cases.json` 后，结果会出现在这里（含 passed/failed 与逐项指标）。"
        />
      ) : (
        <>
          {metrics.length > 0 && (
            <Card className="p-3">
              <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                <span className="text-ink-muted">指标趋势（failed 运行以警示色点标记）</span>
                <select
                  aria-label="趋势指标"
                  value={effectiveMetric}
                  onChange={(event) => setTrendMetric(event.target.value)}
                  className="rounded-btn border border-surface-border bg-surface px-2 py-1"
                >
                  {metrics.map((metric) => (
                    <option key={metric} value={metric}>
                      {metric}
                    </option>
                  ))}
                </select>
              </div>
              <div className="mt-2">
                <TrendChart points={trendPoints} ariaLabel={`${effectiveMetric} 趋势`} />
              </div>
            </Card>
          )}

          {runs.length >= 2 && (
            <Card className="p-3">
              <div className="flex flex-wrap items-end gap-3 text-xs">
                <label className="space-y-1">
                  <span className="block text-ink-muted">基线（旧）</span>
                  <select
                    aria-label="基线运行"
                    value={baselineId}
                    onChange={(event) => setBaselineId(event.target.value)}
                    className="rounded-btn border border-surface-border bg-surface px-2 py-1"
                  >
                    <option value="">选择运行…</option>
                    {runs.map((run) => (
                      <option key={run.id} value={run.id}>
                        {runLabel(run)}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="space-y-1">
                  <span className="block text-ink-muted">候选（新）</span>
                  <select
                    aria-label="候选运行"
                    value={candidateId}
                    onChange={(event) => setCandidateId(event.target.value)}
                    className="rounded-btn border border-surface-border bg-surface px-2 py-1"
                  >
                    <option value="">选择运行…</option>
                    {runs.map((run) => (
                      <option key={run.id} value={run.id}>
                        {runLabel(run)}
                      </option>
                    ))}
                  </select>
                </label>
                <span className="text-ink-muted">delta 正数=候选提升；仅单侧存在的指标记 —</span>
                <button
                  type="button"
                  disabled={!deltas}
                  onClick={() => {
                    if (deltas) {
                      downloadText(
                        timestampedName('regression-diff', 'csv'),
                        metricsDiffToCsv(deltas),
                        'text/csv;charset=utf-8',
                      );
                    }
                  }}
                  className="rounded-tag border border-surface-border px-2 py-0.5 hover:bg-surface-muted disabled:opacity-50"
                >
                  导出 CSV
                </button>
              </div>
              {baseline && candidate && deltas && (
                <table className="mt-3 w-full text-sm" data-testid="metric-diff">
                  <thead>
                    <tr className="border-b border-surface-border text-left text-xs text-ink-muted">
                      <th className="px-2 py-1">指标</th>
                      <th className="px-2 py-1">基线</th>
                      <th className="px-2 py-1">候选</th>
                      <th className="px-2 py-1">Δ</th>
                    </tr>
                  </thead>
                  <tbody>
                    {deltas.map((row) => (
                      <tr key={row.metric} className="border-b border-surface-border last:border-b-0">
                        <td className="px-2 py-1 font-mono text-xs">{row.metric}</td>
                        <td className="px-2 py-1 tabular-nums">
                          {row.baseline !== null ? row.baseline.toFixed(4) : '—'}
                        </td>
                        <td className="px-2 py-1 tabular-nums">
                          {row.candidate !== null ? row.candidate.toFixed(4) : '—'}
                        </td>
                        <td
                          className={`px-2 py-1 tabular-nums ${
                            row.delta === null
                              ? 'text-ink-muted'
                              : row.delta > 0
                                ? 'text-state-ok'
                                : row.delta < 0
                                  ? 'text-state-warn'
                                  : ''
                          }`}
                        >
                          {row.delta === null
                            ? '—'
                            : `${row.delta > 0 ? '+' : ''}${row.delta.toFixed(4)}`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </Card>
          )}

          <ul className="space-y-3">
            {runs.map((run) => (
              <li key={run.id}>
                <Card className="p-4">
                  <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
                    <div className="flex items-center gap-2">
                      <StateBadge state={run.state === 'passed' ? 'DONE' : 'FAILED'} />
                      <span className="font-medium">{run.adapter_ref}</span>
                      {run.state !== 'passed' && run.state !== 'failed' && (
                        <span className="text-xs text-ink-muted">（{run.state}）</span>
                      )}
                    </div>
                    <span className="text-xs text-ink-muted tabular-nums">
                      {new Date(run.created_at).toLocaleString()}
                    </span>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-2 text-xs text-ink-muted">
                    <span title={run.run_id}>run {short(run.run_id)}</span>
                    <span>黄金集 {run.golden_set_name ?? short(run.golden_set_id)}</span>
                    <span>config {short(run.config_digest)}</span>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-2 text-xs">
                    {Object.entries(run.aggregate)
                      .sort(([a], [b]) => a.localeCompare(b))
                      .map(([metric, value]) => (
                        <span
                          key={metric}
                          className="rounded-tag bg-surface-muted px-2 py-0.5 tabular-nums"
                        >
                          {metric} {value.toFixed(4)}
                        </span>
                      ))}
                  </div>
                  {run.violations.length > 0 && (
                    <ul className="mt-2 space-y-1 text-xs text-state-warn" data-testid="violations">
                      {run.violations.map((violation) => (
                        <li key={violation}>[未达基线] {violation}</li>
                      ))}
                    </ul>
                  )}
                  {onLoadDetail && (
                    <button
                      type="button"
                      aria-expanded={expandedId === run.id}
                      onClick={() => toggleDetail(run.id)}
                      className="mt-2 rounded-tag border border-surface-border px-2 py-0.5 text-xs hover:bg-surface-muted"
                    >
                      {expandedId === run.id ? '收起明细' : '查看明细'}
                    </button>
                  )}
                  {expandedId === run.id && (
                    <div
                      className="mt-3 border-t border-surface-border pt-3"
                      data-testid={`run-detail-${run.id}`}
                    >
                      {detailLoadingId === run.id && (
                        <p className="text-xs text-ink-muted">明细加载中…</p>
                      )}
                      {detailError?.runId === run.id && (
                        <p role="alert" className="text-xs text-state-danger">
                          {detailError.message}
                        </p>
                      )}
                      {expandedDetail && (
                        <RegressionCases
                          cases={expandedDetail.cases}
                          exportName={expandedDetail.run_id}
                        />
                      )}
                      {!expandedDetail &&
                        detailLoadingId !== run.id &&
                        detailError?.runId !== run.id && (
                          <p className="text-xs text-ink-muted">尚未加载明细。</p>
                        )}
                    </div>
                  )}
                </Card>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

export function QualityPage() {
  const tenantId = useSessionStore((s) => s.currentTenantId);
  const csrfToken = useSessionStore((s) => s.csrfToken);
  // 过滤由 URL 驱动（可分享/刷新保持）；派生选项在不过滤时展示全部集
  const [searchParams, setSearchParams] = useSearchParams();
  const activeSetId = searchParams.get('golden_set_id');
  const queryClient = useQueryClient();
  const [runState, setRunState] = useState<{ kind: 'ok' | 'error'; message: string } | null>(null);
  const [details, setDetails] = useState<Record<string, RegressionRunDetail>>({});
  const [detailLoadingId, setDetailLoadingId] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<{ runId: string; message: string } | null>(null);

  const loadDetail = async (runId: string) => {
    if (!tenantId || details[runId]) return;
    setDetailError(null);
    setDetailLoadingId(runId);
    try {
      const detail = await regressionRun(runId, tenantId);
      setDetails((prev) => ({ ...prev, [runId]: detail }));
    } catch (err) {
      setDetailError({
        runId,
        message: err instanceof ApiError ? err.message : '明细加载失败',
      });
    } finally {
      setDetailLoadingId((prev) => (prev === runId ? null : prev));
    }
  };

  const runs = useQuery({
    queryKey: ['regression-runs', tenantId, activeSetId],
    enabled: Boolean(tenantId),
    queryFn: () => regressionRuns({ tenantId: tenantId ?? '', goldenSetId: activeSetId }),
  });
  const allRuns = useQuery({
    queryKey: ['regression-runs', tenantId, '__options__'],
    enabled: Boolean(tenantId),
    queryFn: () => regressionRuns({ tenantId: tenantId ?? '', limit: 100 }),
  });

  const run = useMutation({
    mutationFn: (setId: string) =>
      triggerRegressionRun({ goldenSetId: setId, tenantId: tenantId ?? '', csrfToken }),
    onSuccess: (result) => {
      setRunState({
        kind: 'ok',
        message: `运行完成：${result.passed ? 'passed' : 'failed'}（run ${result.run_id.slice(0, 8)}）${result.violations.length > 0 ? ` · ${result.violations.length} 项未达基线` : ''}`,
      });
      void queryClient.invalidateQueries({ queryKey: ['regression-runs', tenantId] });
    },
    onError: (err) => {
      const message =
        err instanceof ApiError && err.status === 403
          ? '需要租户管理员权限'
          : err instanceof ApiError
            ? err.message
            : '运行失败';
      setRunState({ kind: 'error', message });
    },
  });

  if (runs.isPending) return <SkeletonRows />;

  return (
    <QualityBoard
      runs={runs.data?.items ?? []}
      goldenSets={deriveGoldenSets(allRuns.data?.items ?? runs.data?.items ?? [])}
      activeSetId={activeSetId}
      onSetChange={(setId) => {
        const params = new URLSearchParams();
        if (setId) params.set('golden_set_id', setId);
        setSearchParams(params);
        setRunState(null);
      }}
      onRun={(setId) => run.mutate(setId)}
      running={run.isPending}
      runState={runState}
      details={details}
      detailLoadingId={detailLoadingId}
      detailError={detailError}
      onLoadDetail={(runId) => {
        void loadDetail(runId);
      }}
      listError={
        runs.isError ? (runs.error instanceof ApiError ? runs.error : new Error('加载失败')) : null
      }
    />
  );
}
