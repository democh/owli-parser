/**
 * 召回测试台（文档 §9.2）：展示每通道原始分、通道内排名与 RRF 贡献，
 * 低相关/空召回显式警告；检索通道未启用时如实提示（不回退假数据）。
 *
 * RetrievalBoard 为纯展示组件（无数据依赖），便于单测覆盖三种状态：
 * 未启用（capability_not_ready）、空召回、正常结果。
 */
import { useMutation, useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  ApiError,
  request,
  retrievalTest,
  type Dataset,
  type ListPage,
  type RetrievalTestItem,
  type RetrievalTestResponse,
  type RetrievalTunables,
} from '../api/client';
import { Card, EmptyState, ErrorState, SkeletonRows } from '../components/shared';
import { csvField, downloadText, timestampedName } from '../lib/download';
import { useSessionStore } from '../stores/session';

interface BoardProps {
  datasets: Array<{ id: string; name: string }>;
  pending: boolean;
  error: ApiError | Error | null;
  result: RetrievalTestResponse | null;
  /** 对比模式的 B 组结果（与 top_k 相同，仅参数不同——两组可比）。 */
  resultB?: RetrievalTestResponse | null;
  onSubmit: (payload: {
    query: string;
    dataset_ids: string[];
    top_k: number;
    tunables: RetrievalTunables;
    tunablesB?: RetrievalTunables;
  }) => void;
}

/** 高级参数输入 → tunables（留空取服务端配置；仅收集有效数值）。 */
export function buildTunables(input: {
  rrfK: string;
  perChannel: string;
  denseWeight: string;
  lexicalWeight: string;
  threshold: string;
}): RetrievalTunables {
  const tunables: RetrievalTunables = {};
  const positive = (value: string): number | null => {
    const parsed = Number(value);
    return value.trim() !== '' && Number.isFinite(parsed) && parsed > 0 ? parsed : null;
  };
  const nonNegative = (value: string): number | null => {
    const parsed = Number(value);
    return value.trim() !== '' && Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
  };
  const rrfK = positive(input.rrfK);
  if (rrfK !== null) tunables.rrf_k = rrfK;
  const perChannel = positive(input.perChannel);
  if (perChannel !== null) tunables.per_channel_limit = perChannel;
  const threshold = nonNegative(input.threshold);
  if (threshold !== null) tunables.min_relevance_threshold = threshold;
  const dense = nonNegative(input.denseWeight);
  const lexical = nonNegative(input.lexicalWeight);
  if (dense !== null || lexical !== null) {
    tunables.channel_weights = {
      ...(dense !== null ? { dense } : {}),
      ...(lexical !== null ? { lexical } : {}),
    };
  }
  return tunables;
}

function channelEvidence(item: RetrievalTestItem): Array<{
  channel: string;
  rank: number;
  rawScore: number;
  contribution: number;
}> {
  return Object.entries(item.channel_ranks)
    .map(([channel, rank]) => ({
      channel,
      rank,
      rawScore: item.channel_raw_scores[channel] ?? 0,
      contribution: item.channel_contributions[channel] ?? 0,
    }))
    .sort((a, b) => a.channel.localeCompare(b.channel));
}

function chunkLabel(item: RetrievalTestItem): string {
  const chunkId = item.payload['chunk_id'];
  const text = typeof chunkId === 'string' ? chunkId : item.id;
  return text.length > 12 ? `${text.slice(0, 8)}…${text.slice(-4)}` : text;
}

function documentLink(item: RetrievalTestItem): string | null {
  const documentId = item.payload['document_id'];
  return typeof documentId === 'string' && documentId ? `/documents/${documentId}` : null;
}

/** 对比行：同一 chunk 在 A/B 两组参数下的排名与 RRF 分。 */
export interface RetrievalDiffRow {
  chunkId: string;
  rankA: number | null;
  rankB: number | null;
  rrfA: number;
  rrfB: number;
}

export interface RetrievalDiff {
  rows: RetrievalDiffRow[];
  onlyA: number;
  onlyB: number;
  common: number;
  /** 共有项的平均排名变化（rankA - rankB；正数表示 B 组整体前移）。 */
  meanRankShift: number | null;
}

function diffKey(item: RetrievalTestItem): string {
  const chunkId = item.payload['chunk_id'];
  return typeof chunkId === 'string' && chunkId ? chunkId : item.id;
}

/** 两次检索结果的并排对比（纯函数）：仅 A/仅 B/共有与平均排名变化。 */
export function diffRetrievals(a: RetrievalTestResponse, b: RetrievalTestResponse): RetrievalDiff {
  const mapA = new Map<string, { rank: number; rrf: number }>();
  a.items.forEach((item, index) => {
    mapA.set(diffKey(item), { rank: index + 1, rrf: item.rrf_score });
  });
  const mapB = new Map<string, { rank: number; rrf: number }>();
  b.items.forEach((item, index) => {
    mapB.set(diffKey(item), { rank: index + 1, rrf: item.rrf_score });
  });

  const keys = new Set([...mapA.keys(), ...mapB.keys()]);
  const rows: RetrievalDiffRow[] = [...keys].map((key) => ({
    chunkId: key,
    rankA: mapA.get(key)?.rank ?? null,
    rankB: mapB.get(key)?.rank ?? null,
    rrfA: mapA.get(key)?.rrf ?? 0,
    rrfB: mapB.get(key)?.rrf ?? 0,
  }));
  rows.sort(
    (x, y) =>
      Math.min(x.rankA ?? Number.POSITIVE_INFINITY, x.rankB ?? Number.POSITIVE_INFINITY) -
        Math.min(y.rankA ?? Number.POSITIVE_INFINITY, y.rankB ?? Number.POSITIVE_INFINITY) ||
      x.chunkId.localeCompare(y.chunkId),
  );

  const commonRows = rows.filter((row) => row.rankA !== null && row.rankB !== null);
  const meanRankShift =
    commonRows.length > 0
      ? commonRows.reduce((sum, row) => sum + ((row.rankA ?? 0) - (row.rankB ?? 0)), 0) /
        commonRows.length
      : null;
  return {
    rows,
    onlyA: rows.filter((row) => row.rankA !== null && row.rankB === null).length,
    onlyB: rows.filter((row) => row.rankB !== null && row.rankA === null).length,
    common: commonRows.length,
    meanRankShift,
  };
}

function shortId(value: string): string {
  return value.length > 12 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
}

/** 对比 CSV（归档/复盘）：仅 A/仅 B 的无排名侧留空。 */
export function diffToCsv(diff: RetrievalDiff): string {
  const lines = ['chunk_id,rank_a,rank_b,rrf_a,rrf_b,delta_rank'];
  for (const row of diff.rows) {
    const delta =
      row.rankA !== null && row.rankB !== null ? String(row.rankA - row.rankB) : '';
    lines.push(
      [
        csvField(row.chunkId),
        csvField(row.rankA),
        csvField(row.rankB),
        row.rrfA.toFixed(6),
        row.rrfB.toFixed(6),
        delta,
      ].join(','),
    );
  }
  return `${lines.join('\n')}\n`;
}

/** 对比 JSON（含两组实际生效参数与摘要，供复盘归档）。 */
export function diffToJson(a: RetrievalTestResponse, b: RetrievalTestResponse, diff: RetrievalDiff): string {
  return `${JSON.stringify(
    {
      payload: {
        a: { applied_params: a.applied_params, low_relevance: a.low_relevance },
        b: { applied_params: b.applied_params, low_relevance: b.low_relevance },
      },
      summary: {
        only_a: diff.onlyA,
        only_b: diff.onlyB,
        common: diff.common,
        mean_rank_shift: diff.meanRankShift,
      },
      rows: diff.rows,
    },
    null,
    2,
  )}\n`;
}

/** 对比视图：摘要 + 逐项并排表（Δ 正数=B 组前移）+ 导出。 */
export function RetrievalDiffView({ a, b }: { a: RetrievalTestResponse; b: RetrievalTestResponse }) {
  const diff = diffRetrievals(a, b);
  return (
    <Card className="overflow-x-auto">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-surface-border px-4 py-2 text-xs text-ink-muted">
        <span data-testid="diff-summary">
          仅 A 命中 {diff.onlyA} 项 · 仅 B 命中 {diff.onlyB} 项 · 共有 {diff.common} 项
          {diff.meanRankShift !== null && (
            <span className="ml-2 tabular-nums">
              平均排名变化 {diff.meanRankShift >= 0 ? '+' : ''}
              {diff.meanRankShift.toFixed(2)}（正数=B 组前移）
            </span>
          )}
        </span>
        <span className="flex gap-2">
          <button
            type="button"
            onClick={() => downloadText(timestampedName('retrieval-diff', 'csv'), diffToCsv(diff), 'text/csv;charset=utf-8')}
            className="rounded-tag border border-surface-border px-2 py-0.5 hover:bg-surface-muted"
          >
            导出 CSV
          </button>
          <button
            type="button"
            onClick={() =>
              downloadText(
                timestampedName('retrieval-diff', 'json'),
                diffToJson(a, b, diff),
                'application/json;charset=utf-8',
              )
            }
            className="rounded-tag border border-surface-border px-2 py-0.5 hover:bg-surface-muted"
          >
            导出 JSON
          </button>
        </span>
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-surface-border text-left text-xs text-ink-muted">
            <th className="px-3 py-2">Chunk</th>
            <th className="px-3 py-2">A 排名 / RRF</th>
            <th className="px-3 py-2">B 排名 / RRF</th>
            <th className="px-3 py-2">Δ 排名</th>
          </tr>
        </thead>
        <tbody>
          {diff.rows.map((row) => {
            const delta =
              row.rankA !== null && row.rankB !== null ? row.rankA - row.rankB : null;
            return (
              <tr key={row.chunkId} className="border-b border-surface-border last:border-b-0">
                <td className="px-3 py-2 font-mono text-xs" title={row.chunkId}>
                  {shortId(row.chunkId)}
                </td>
                <td className="px-3 py-2 tabular-nums">
                  {row.rankA !== null ? `#${row.rankA} · ${row.rrfA.toFixed(4)}` : '—'}
                </td>
                <td className="px-3 py-2 tabular-nums">
                  {row.rankB !== null ? `#${row.rankB} · ${row.rrfB.toFixed(4)}` : '—'}
                </td>
                <td
                  className={`px-3 py-2 tabular-nums ${
                    delta === null
                      ? 'text-ink-muted'
                      : delta > 0
                        ? 'text-state-ok'
                        : delta < 0
                          ? 'text-state-warn'
                          : ''
                  }`}
                >
                  {delta === null ? (row.rankB === null ? '仅 A' : '仅 B') : delta > 0 ? `+${delta}` : String(delta)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </Card>
  );
}

export function RetrievalBoard({ datasets, pending, error, result, resultB, onSubmit }: BoardProps) {
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<string[]>([]);
  const [topK, setTopK] = useState(10);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [rrfK, setRrfK] = useState('');
  const [perChannel, setPerChannel] = useState('');
  const [denseWeight, setDenseWeight] = useState('');
  const [lexicalWeight, setLexicalWeight] = useState('');
  const [threshold, setThreshold] = useState('');
  // A/B 对比模式：两组参数各跑一次，结果并排 diff（top_k 共享，保证可比）
  const [compareMode, setCompareMode] = useState(false);
  const [rrfKB, setRrfKB] = useState('');
  const [perChannelB, setPerChannelB] = useState('');
  const [denseWeightB, setDenseWeightB] = useState('');
  const [lexicalWeightB, setLexicalWeightB] = useState('');
  const [thresholdB, setThresholdB] = useState('');
  const showDiff = Boolean(result && resultB);

  const isCapabilityNotReady =
    error instanceof ApiError &&
    (error.status === 503 || error.code === 'capability_not_ready');

  const toggleDataset = (id: string) => {
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  };

  return (
    <div className="mx-auto max-w-5xl space-y-4">
      <div>
        <h1 className="text-lg font-semibold">召回测试台</h1>
        <p className="mt-1 text-xs text-ink-muted">
          展示每个通道的原始得分、通道内排名与 RRF 贡献；低相关或空召回会显式提示，
          不伪造相关性结论。
        </p>
      </div>

      <Card className="p-4">
        <div className="space-y-3">
          <div>
            <label className="text-xs text-ink-muted" htmlFor="retrieval-query">
              查询词
            </label>
            <input
              id="retrieval-query"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="输入查询内容"
              className="mt-1 w-full rounded-btn border border-surface-border bg-surface px-3 py-2 text-sm"
            />
          </div>
          <div>
            <div className="text-xs text-ink-muted">数据集范围（前置过滤）</div>
            <div className="mt-1 flex flex-wrap gap-2">
              {datasets.length === 0 && (
                <span className="text-xs text-ink-muted">暂无数据集</span>
              )}
              {datasets.map((ds) => (
                <label
                  key={ds.id}
                  className="flex items-center gap-1.5 rounded-tag border border-surface-border px-2 py-1 text-xs"
                >
                  <input
                    type="checkbox"
                    checked={selected.includes(ds.id)}
                    onChange={() => toggleDataset(ds.id)}
                  />
                  {ds.name}
                </label>
              ))}
            </div>
          </div>
          <div className="flex items-end gap-3">
            <div>
              <label className="text-xs text-ink-muted" htmlFor="retrieval-topk">
                TopK
              </label>
              <input
                id="retrieval-topk"
                type="number"
                min={1}
                max={100}
                value={topK}
                onChange={(e) => setTopK(Math.max(1, Number(e.target.value) || 1))}
                className="mt-1 w-24 rounded-btn border border-surface-border bg-surface px-3 py-2 text-sm"
              />
            </div>
            <button
              type="button"
              disabled={pending || !query.trim() || selected.length === 0}
              onClick={() =>
                onSubmit({
                  query: query.trim(),
                  dataset_ids: selected,
                  top_k: topK,
                  tunables: buildTunables({ rrfK, perChannel, denseWeight, lexicalWeight, threshold }),
                  ...(compareMode
                    ? {
                        tunablesB: buildTunables({
                          rrfK: rrfKB,
                          perChannel: perChannelB,
                          denseWeight: denseWeightB,
                          lexicalWeight: lexicalWeightB,
                          threshold: thresholdB,
                        }),
                      }
                    : {}),
                })
              }
              className="rounded-btn bg-brand px-4 py-2 text-sm text-white disabled:opacity-50"
            >
              {pending ? '检索中…' : compareMode ? '执行对比' : '执行检索'}
            </button>
            <button
              type="button"
              onClick={() => setShowAdvanced((v) => !v)}
              className="rounded-btn border border-surface-border px-3 py-2 text-xs hover:bg-surface-muted"
            >
              {showAdvanced || compareMode ? '收起高级参数' : '高级参数'}
            </button>
            <label className="flex items-center gap-1 text-xs text-ink-muted">
              <input
                type="checkbox"
                checked={compareMode}
                onChange={(event) => setCompareMode(event.target.checked)}
              />
              A/B 对比模式
            </label>
          </div>
          {(showAdvanced || compareMode) && (
            <div className="grid grid-cols-2 gap-3 rounded-card border border-surface-border bg-surface-muted p-3 text-xs md:grid-cols-5">
              <label className="space-y-1">
                <span className="block text-ink-muted">RRF k（≥1）</span>
                <input
                  aria-label="RRF k"
                  value={rrfK}
                  onChange={(e) => setRrfK(e.target.value)}
                  placeholder="缺省"
                  className="w-full rounded-btn border border-surface-border bg-surface px-2 py-1"
                />
              </label>
              <label className="space-y-1">
                <span className="block text-ink-muted">每通道上限</span>
                <input
                  aria-label="每通道上限"
                  value={perChannel}
                  onChange={(e) => setPerChannel(e.target.value)}
                  placeholder="缺省"
                  className="w-full rounded-btn border border-surface-border bg-surface px-2 py-1"
                />
              </label>
              <label className="space-y-1">
                <span className="block text-ink-muted">dense 权重</span>
                <input
                  aria-label="dense 权重"
                  value={denseWeight}
                  onChange={(e) => setDenseWeight(e.target.value)}
                  placeholder="缺省"
                  className="w-full rounded-btn border border-surface-border bg-surface px-2 py-1"
                />
              </label>
              <label className="space-y-1">
                <span className="block text-ink-muted">lexical 权重</span>
                <input
                  aria-label="lexical 权重"
                  value={lexicalWeight}
                  onChange={(e) => setLexicalWeight(e.target.value)}
                  placeholder="缺省"
                  className="w-full rounded-btn border border-surface-border bg-surface px-2 py-1"
                />
              </label>
              <label className="space-y-1">
                <span className="block text-ink-muted">低相关阈值</span>
                <input
                  aria-label="低相关阈值"
                  value={threshold}
                  onChange={(e) => setThreshold(e.target.value)}
                  placeholder="缺省"
                  className="w-full rounded-btn border border-surface-border bg-surface px-2 py-1"
                />
              </label>
            </div>
          )}
          {compareMode && (
            <div className="space-y-2 rounded-card border border-brand/40 bg-brand-soft/40 p-3 text-xs">
              <div className="text-ink-muted">B 组参数（与 A 组同 top_k，可比）</div>
              <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
                <label className="space-y-1">
                  <span className="block text-ink-muted">RRF k（≥1）</span>
                  <input
                    aria-label="B 组 RRF k"
                    value={rrfKB}
                    onChange={(e) => setRrfKB(e.target.value)}
                    placeholder="缺省"
                    className="w-full rounded-btn border border-surface-border bg-surface px-2 py-1"
                  />
                </label>
                <label className="space-y-1">
                  <span className="block text-ink-muted">每通道上限</span>
                  <input
                    aria-label="B 组每通道上限"
                    value={perChannelB}
                    onChange={(e) => setPerChannelB(e.target.value)}
                    placeholder="缺省"
                    className="w-full rounded-btn border border-surface-border bg-surface px-2 py-1"
                  />
                </label>
                <label className="space-y-1">
                  <span className="block text-ink-muted">dense 权重</span>
                  <input
                    aria-label="B 组 dense 权重"
                    value={denseWeightB}
                    onChange={(e) => setDenseWeightB(e.target.value)}
                    placeholder="缺省"
                    className="w-full rounded-btn border border-surface-border bg-surface px-2 py-1"
                  />
                </label>
                <label className="space-y-1">
                  <span className="block text-ink-muted">lexical 权重</span>
                  <input
                    aria-label="B 组 lexical 权重"
                    value={lexicalWeightB}
                    onChange={(e) => setLexicalWeightB(e.target.value)}
                    placeholder="缺省"
                    className="w-full rounded-btn border border-surface-border bg-surface px-2 py-1"
                  />
                </label>
                <label className="space-y-1">
                  <span className="block text-ink-muted">低相关阈值</span>
                  <input
                    aria-label="B 组低相关阈值"
                    value={thresholdB}
                    onChange={(e) => setThresholdB(e.target.value)}
                    placeholder="缺省"
                    className="w-full rounded-btn border border-surface-border bg-surface px-2 py-1"
                  />
                </label>
              </div>
            </div>
          )}
          {selected.length === 0 && (
            <p className="text-xs text-ink-muted">检索必须指定数据集范围。</p>
          )}
        </div>
      </Card>

      {isCapabilityNotReady && (
        <Card className="p-4" >
          <div data-testid="retrieval-not-ready" className="text-sm">
            <p className="font-medium">检索通道未启用</p>
            <p className="mt-1 text-xs text-ink-muted">
              服务端返回 capability_not_ready（retrieval.enabled=false，M3 里程碑）。
              在索引验证完成前，回答不会消费未验证的召回结果。
            </p>
          </div>
        </Card>
      )}

      {error && !isCapabilityNotReady && (
        <ErrorState message={error.message || '检索失败'} />
      )}

      {showDiff && result && resultB && <RetrievalDiffView a={result} b={resultB} />}

      {result && !showDiff && (
        <>
          {(result.low_relevance || result.warnings.length > 0) && (
            <Card className="p-3">
              <ul className="space-y-1 text-xs" data-testid="retrieval-warnings">
                {result.low_relevance && (
                  <li role="alert" className="text-state-warn">
                    所有召回低于相关性阈值：结果可能不相关（low_relevance=true）
                  </li>
                )}
                {result.warnings.map((warning) => (
                  <li key={warning} className="text-state-warn">
                    {warning}
                  </li>
                ))}
              </ul>
            </Card>
          )}

          <Card className="p-3">
            <div className="flex flex-wrap items-center gap-2 text-xs text-ink-muted">
              <span>参与通道：</span>
              {result.channels_used.length === 0 ? (
                <span>无</span>
              ) : (
                result.channels_used.map((channel) => (
                  <span
                    key={channel}
                    className="rounded-tag bg-brand-soft px-2 py-0.5 text-brand"
                  >
                    {channel}
                  </span>
                ))
              )}
              <span className="ml-2">过滤条件：租户 {result.applied_filters.tenant_id.slice(0, 8)}…</span>
              {(result.applied_filters.dataset_ids ?? []).map((id) => (
                <span key={id} className="rounded-tag bg-surface-muted px-2 py-0.5">
                  数据集 {id.slice(0, 8)}
                </span>
              ))}
            </div>
            <div
              data-testid="applied-params"
              className="mt-2 flex flex-wrap gap-2 text-xs text-ink-muted"
            >
              <span className="rounded-tag bg-surface-muted px-2 py-0.5 tabular-nums">
                RRF k={result.applied_params.rrf_k}
              </span>
              {Object.entries(result.applied_params.channel_weights).map(([channel, weight]) => (
                <span key={channel} className="rounded-tag bg-surface-muted px-2 py-0.5 tabular-nums">
                  权重 {channel}={weight}
                </span>
              ))}
              <span className="rounded-tag bg-surface-muted px-2 py-0.5 tabular-nums">
                每通道={result.applied_params.per_channel_limit} · 候选上限=
                {result.applied_params.candidate_limit}
              </span>
              <span className="rounded-tag bg-surface-muted px-2 py-0.5 tabular-nums">
                阈值={result.applied_params.min_relevance_threshold}
              </span>
              {result.applied_params.rerank_applied && (
                <span
                  data-testid="rerank-applied"
                  className="rounded-tag bg-brand-soft px-2 py-0.5 text-brand"
                >
                  rerank 已生效
                </span>
              )}
            </div>
          </Card>

          {result.items.length === 0 ? (
            <EmptyState
              title="无召回结果"
              hint="没有候选通过前置过滤与权威校验（或索引中暂无数据）。"
            />
          ) : (
            <Card className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-surface-border text-left text-xs text-ink-muted">
                    <th className="px-3 py-2">#</th>
                    <th className="px-3 py-2">Chunk</th>
                    <th className="px-3 py-2">RRF 分</th>
                    <th className="px-3 py-2">通道证据（rank · 原始分 · 贡献）</th>
                    <th className="px-3 py-2">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {result.items.map((item, index) => {
                    const link = documentLink(item);
                    return (
                      <tr key={`${item.id}-${index}`} className="border-b border-surface-border last:border-b-0">
                        <td className="px-3 py-2 tabular-nums text-ink-muted">{index + 1}</td>
                        <td className="px-3 py-2 font-mono text-xs" title={item.id}>
                          {chunkLabel(item)}
                        </td>
                        <td className="px-3 py-2 tabular-nums">{item.rrf_score.toFixed(4)}</td>
                        <td className="px-3 py-2">
                          <div className="flex flex-wrap gap-2 text-xs">
                            {channelEvidence(item).map((evidence) => (
                              <span
                                key={evidence.channel}
                                className="rounded-tag bg-surface-muted px-2 py-0.5 tabular-nums"
                              >
                                {evidence.channel} #{evidence.rank} ·{' '}
                                {evidence.rawScore.toFixed(4)} · +
                                {evidence.contribution.toFixed(4)}
                              </span>
                            ))}
                          </div>
                        </td>
                        <td className="px-3 py-2">
                          {link ? (
                            <Link to={link} className="text-xs text-brand underline">
                              打开工作台
                            </Link>
                          ) : (
                            <span className="text-xs text-ink-muted">—</span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </Card>
          )}
        </>
      )}
    </div>
  );
}

export function RetrievalPage() {
  const tenantId = useSessionStore((s) => s.currentTenantId);
  const csrfToken = useSessionStore((s) => s.csrfToken);
  const [result, setResult] = useState<RetrievalTestResponse | null>(null);
  const [resultB, setResultB] = useState<RetrievalTestResponse | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);

  const datasets = useQuery({
    queryKey: ['datasets', tenantId],
    enabled: Boolean(tenantId),
    queryFn: () => request<ListPage<Dataset>>('/datasets?limit=100', { tenantId }),
  });

  const search = useMutation({
    mutationFn: async (payload: {
      query: string;
      dataset_ids: string[];
      top_k: number;
      tunables: RetrievalTunables;
      tunablesB?: RetrievalTunables;
    }) => {
      const base = {
        query: payload.query,
        dataset_ids: payload.dataset_ids,
        tenantId: tenantId ?? '',
        csrfToken,
      };
      const first = await retrievalTest({
        ...base,
        tunables: { top_k: payload.top_k, ...payload.tunables },
      });
      if (payload.tunablesB === undefined) {
        return { first, second: null };
      }
      const second = await retrievalTest({
        ...base,
        tunables: { top_k: payload.top_k, ...payload.tunablesB },
      });
      return { first, second };
    },
    onSuccess: ({ first, second }) => {
      setResult(first);
      setResultB(second);
      setError(null);
    },
    onError: (err) => {
      setResult(null);
      setResultB(null);
      setError(err instanceof Error ? err : new Error(String(err)));
    },
  });

  if (datasets.isPending) return <SkeletonRows />;

  return (
    <RetrievalBoard
      datasets={(datasets.data?.items ?? []).map((ds) => ({ id: ds.id, name: ds.name }))}
      pending={search.isPending}
      error={error}
      result={result}
      resultB={resultB}
      onSubmit={(payload) => search.mutate(payload)}
    />
  );
}
