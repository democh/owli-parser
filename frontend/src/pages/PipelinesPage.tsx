/**
 * 解析管道配置页（版本化）：查看 / 编辑 YAML / 保存（If-Match）/ 执行计划预览。
 *
 * - 保存为新版本（不覆盖历史）；对内置 default 保存即租户级覆盖（提交解析消费）；
 * - 校验失败（422）如实展示服务端信息，不改动本地草稿；
 * - 校验阶段编译（digest 由服务端计算），前端不做 YAML 语义判断。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { lazy, Suspense, useState } from 'react';
import {
  adapterHeartbeat,
  adapters as fetchAdapters,
  ApiError,
  pipelineDetail,
  pipelineDryRun,
  pipelineExperiment,
  pipelineExperimentReport,
  pipelines,
  request,
  registerAdapter,
  savePipeline,
  savePipelineExperiment,
  validatePipeline,
  type AdapterInfo,
  type AdapterRegisterInput,
  type DocumentItem,
  type ListPage,
  type PipelineDetail,
  type PipelineDryRunResult,
  type PipelineExperiment,
  type PipelineExperimentReport,
  type PipelineSummary,
  type PipelineValidationResult,
} from '../api/client';
import { AdapterHealthStrip } from '../components/AdapterHealthStrip';
import { AdapterRegistryPanel } from '../components/AdapterRegistryPanel';
import { Card, ErrorState, SkeletonRows, StateBadge } from '../components/shared';
import { useSessionStore } from '../stores/session';

// 画布与表单仅在交互后加载：避免把 ReactFlow/YAML 打进首屏包
const PipelineCanvas = lazy(() =>
  import('../components/PipelineCanvas').then((module) => ({ default: module.PipelineCanvas })),
);
const PipelineForm = lazy(() =>
  import('../components/PipelineForm').then((module) => ({ default: module.PipelineForm })),
);
const PipelineCanvasEditor = lazy(() =>
  import('../components/PipelineCanvasEditor').then((module) => ({
    default: module.PipelineCanvasEditor,
  })),
);

interface BoardProps {
  items: PipelineSummary[];
  detail: PipelineDetail | null;
  detailPending: boolean;
  documents: Array<{ id: string; filename: string; versionId: string }>;
  /** 可选 adapter 名（表单模式候选；未加载时仍可手动输入）。 */
  adapterNames?: string[];
  /** adapter 健康清单（内置 + 外部注册；用于健康条）。 */
  adapterInfo?: AdapterInfo[];
  isPlatformAdmin?: boolean;
  adapterRegistering?: boolean;
  adapterHeartbeatName?: string | null;
  adapterActionError?: string | null;
  onRegisterAdapter?: (values: AdapterRegisterInput) => void;
  onAdapterHeartbeat?: (name: string) => void;
  onSelect: (name: string) => void;
  onSave: (name: string, yamlText: string, expectedVersion: number) => void;
  saving: boolean;
  saveState: { kind: 'ok' | 'error'; message: string } | null;
  listError: Error | null;
  onValidate: (name: string, yamlText: string) => void;
  validating: boolean;
  validation: PipelineValidationResult | null;
  onDryRun: (payload: { name: string; yamlText: string; versionId: string }) => void;
  dryRunning: boolean;
  dryRun: PipelineDryRunResult | null;
  dryRunError: string | null;
  experiment?: PipelineExperiment | null;
  experimentPending?: boolean;
  experimentSaving?: boolean;
  experimentError?: string | null;
  experimentReport?: PipelineExperimentReport | null;
  experimentReportPending?: boolean;
  onSaveExperiment?: (payload: {
    pipelineName: string;
    name: string;
    variantAYaml: string;
    variantBYaml: string;
    trafficPercent: number;
    active: boolean;
  }) => void;
}

function shortDigest(digest: string): string {
  return digest.length > 12 ? `${digest.slice(0, 12)}…` : digest;
}

const SOURCE_LABELS: Record<PipelineDryRunResult['pipeline']['source'], string> = {
  draft: '草稿',
  tenant: '租户配置',
  builtin: '内置',
};

/** dry-run 结果：格式/生效 pipeline/命中 adapter/降级链/预检特征/警告。 */
export function DryRunView({ result }: { result: PipelineDryRunResult }) {
  const features = result.features;
  return (
    <div
      data-testid="pipeline-dry-run"
      className="mt-2 space-y-2 rounded-card border border-surface-border bg-surface-muted/40 p-3 text-xs"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded-tag bg-brand-soft px-2 py-0.5 text-brand">{result.format}</span>
        <span className="tabular-nums">
          pipeline {result.pipeline.name} v{result.pipeline.version}（
          {SOURCE_LABELS[result.pipeline.source]}）
        </span>
        {result.ok ? (
          <span
            data-testid="dry-run-adapter"
            className="rounded-tag bg-surface px-2 py-0.5 text-state-ok"
          >
            → {result.route.adapter}
          </span>
        ) : (
          <span
            data-testid="dry-run-no-route"
            className="rounded-tag bg-surface px-2 py-0.5 text-state-danger"
          >
            无可用解析分支
          </span>
        )}
      </div>
      <div className="flex flex-wrap gap-3 text-ink-muted">
        <span>扩展名={features.extension || '未知'}</span>
        <span>mime={features.mime ?? '未知'}</span>
        <span>页数={features.page_count ?? '—'}</span>
        <span>
          文本层={features.text_layer_ratio !== null ? features.text_layer_ratio.toFixed(2) : '—'}
        </span>
      </div>
      <p data-testid="dry-run-reason">{result.route.reason}</p>
      {result.route.skipped.length > 0 && (
        <ul data-testid="dry-run-skipped" className="space-y-1">
          {result.route.skipped.map((item) => (
            <li key={item.adapter} className="text-ink-muted">
              降级：{item.adapter} — {item.reason}
            </li>
          ))}
        </ul>
      )}
      {result.warnings.length > 0 && (
        <ul role="alert" data-testid="dry-run-warnings" className="space-y-1 text-state-warn">
          {result.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function PipelinesBoard({
  items,
  detail,
  detailPending,
  documents,
  adapterNames = [],
  adapterInfo = [],
  isPlatformAdmin = false,
  adapterRegistering = false,
  adapterHeartbeatName = null,
  adapterActionError = null,
  onRegisterAdapter = () => undefined,
  onAdapterHeartbeat = () => undefined,
  onSelect,
  onSave,
  saving,
  saveState,
  listError,
  onValidate,
  validating,
  validation,
  onDryRun,
  dryRunning,
  dryRun,
  dryRunError,
  experiment = null,
  experimentPending = false,
  experimentSaving = false,
  experimentError = null,
  experimentReport = null,
  experimentReportPending = false,
  onSaveExperiment = () => undefined,
}: BoardProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [sampleDocId, setSampleDocId] = useState('');
  const [experimentOpen, setExperimentOpen] = useState(false);
  const [expName, setExpName] = useState('');
  const [expTraffic, setExpTraffic] = useState(50);
  const [expActive, setExpActive] = useState(true);
  const [expAYaml, setExpAYaml] = useState('');
  const [expBYaml, setExpBYaml] = useState('');
  const [canvasOpen, setCanvasOpen] = useState(false);
  const [editorMode, setEditorMode] = useState<'yaml' | 'form' | 'canvas'>('yaml');
  // 质量指标键：A/B 并集（仅比例类指标），用于并排展示
  const qualityKeys = experimentReport
    ? [
        ...new Set(
          (['A', 'B'] as const).flatMap((variant) =>
            Object.keys(experimentReport.variants[variant].quality_metrics ?? {}),
          ),
        ),
      ].sort()
    : [];

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <div>
        <h1 className="text-lg font-semibold">解析管道</h1>
        <p className="mt-1 text-xs text-ink-muted">
          版本化配置：保存生成新版本（If-Match 防并发覆盖）；提交解析时冻结进任务快照，
          worker 只按快照执行。对内置 default 保存即为租户级覆盖。
        </p>
      </div>

      <AdapterHealthStrip items={adapterInfo} />
      <AdapterRegistryPanel
        items={adapterInfo}
        visible={isPlatformAdmin}
        registering={adapterRegistering}
        heartbeatName={adapterHeartbeatName}
        error={adapterActionError}
        onRegister={onRegisterAdapter}
        onHeartbeat={onAdapterHeartbeat}
      />

      {listError && <ErrorState message={listError.message || '管道列表加载失败'} />}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="p-2">
          <ul className="divide-y divide-surface-border text-sm">
            {items.map((item) => (
              <li key={item.name}>
                <button
                  type="button"
                  onClick={() => {
                    onSelect(item.name);
                    setEditing(false);
                  }}
                  className={`w-full px-3 py-2 text-left hover:bg-surface-muted ${
                    detail?.name === item.name ? 'bg-brand-soft' : ''
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium">{item.name}</span>
                    <span
                      className={`rounded-tag px-2 py-0.5 text-xs ${
                        item.source === 'tenant'
                          ? 'bg-brand-soft text-brand'
                          : 'bg-surface-muted text-ink-muted'
                      }`}
                    >
                      {item.source === 'tenant' ? '租户覆盖' : '内置'}
                    </span>
                  </div>
                  <div className="mt-1 flex gap-3 text-xs text-ink-muted tabular-nums">
                    <span>v{item.version}</span>
                    <span title={item.digest}>digest {shortDigest(item.digest)}</span>
                  </div>
                </button>
              </li>
            ))}
            {items.length === 0 && (
              <li className="px-3 py-2 text-xs text-ink-muted">暂无管道</li>
            )}
          </ul>
        </Card>

        <Card className="p-4 lg:col-span-2">
          {detailPending && <SkeletonRows rows={4} />}
          {!detailPending && detail === null && (
            <p className="text-xs text-ink-muted">选择左侧管道查看配置。</p>
          )}
          {!detailPending && detail !== null && (
            <div className="space-y-3 text-sm">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{detail.name}</span>
                  <StateBadge state={detail.active ? 'READY' : 'CANCELLED'} />
                  <span className="text-xs text-ink-muted tabular-nums">
                    v{detail.version} · {detail.source === 'tenant' ? '租户覆盖' : '内置'}
                  </span>
                </div>
                <div className="flex gap-2">
                  {!editing ? (
                    <button
                      type="button"
                      onClick={() => {
                        setEditing(true);
                        setEditorMode('yaml');
                        setDraft(detail.yaml);
                      }}
                      className="rounded-btn border border-surface-border px-3 py-1 text-xs hover:bg-surface-muted"
                    >
                      编辑
                    </button>
                  ) : (
                    <>
                      <button
                        type="button"
                        disabled={validating}
                        onClick={() => onValidate(detail.name, draft)}
                        className="rounded-btn border border-surface-border px-3 py-1 text-xs hover:bg-surface-muted disabled:opacity-50"
                      >
                        {validating ? '校验中…' : '校验草稿'}
                      </button>
                      <button
                        type="button"
                        disabled={saving}
                        onClick={() => onSave(detail.name, draft, detail.version)}
                        className="rounded-btn bg-brand px-3 py-1 text-xs text-white disabled:opacity-50"
                      >
                        {saving ? '保存中…' : `保存为新版本（If-Match v${detail.version}）`}
                      </button>
                      <button
                        type="button"
                        onClick={() => setEditing(false)}
                        className="rounded-btn border border-surface-border px-3 py-1 text-xs"
                      >
                        取消
                      </button>
                    </>
                  )}
                </div>
              </div>

              {saveState && (
                <p
                  role={saveState.kind === 'error' ? 'alert' : 'status'}
                  data-testid="pipeline-save-state"
                  className={`text-xs ${saveState.kind === 'error' ? 'text-state-danger' : 'text-state-ok'}`}
                >
                  {saveState.message}
                </p>
              )}

              {validation && (
                <div
                  data-testid="pipeline-validation"
                  role={validation.ok ? 'status' : 'alert'}
                  className={`rounded-card border border-surface-border p-2 text-xs ${
                    validation.ok ? 'text-state-ok' : 'text-state-danger'
                  }`}
                >
                  {validation.ok ? (
                    <span>
                      草稿校验通过
                      {validation.version !== undefined ? ` · v${validation.version}` : ''}
                      {validation.digest ? ` · digest ${shortDigest(validation.digest)}` : ''}
                    </span>
                  ) : (
                    <ul className="list-inside list-disc space-y-1">
                      {validation.errors.map((error) => (
                        <li key={error}>{error}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              {editing ? (
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <button
                      type="button"
                      aria-pressed={editorMode === 'yaml'}
                      onClick={() => setEditorMode('yaml')}
                      className={`rounded-tag px-2 py-0.5 ${
                        editorMode === 'yaml'
                          ? 'bg-brand text-white'
                          : 'border border-surface-border hover:bg-surface-muted'
                      }`}
                    >
                      YAML
                    </button>
                    <button
                      type="button"
                      aria-pressed={editorMode === 'form'}
                      onClick={() => setEditorMode('form')}
                      className={`rounded-tag px-2 py-0.5 ${
                        editorMode === 'form'
                          ? 'bg-brand text-white'
                          : 'border border-surface-border hover:bg-surface-muted'
                      }`}
                    >
                      表单
                    </button>
                    <button
                      type="button"
                      aria-pressed={editorMode === 'canvas'}
                      onClick={() => setEditorMode('canvas')}
                      className={`rounded-tag px-2 py-0.5 ${
                        editorMode === 'canvas'
                          ? 'bg-brand text-white'
                          : 'border border-surface-border hover:bg-surface-muted'
                      }`}
                    >
                      画布
                    </button>
                    <span className="text-ink-muted">
                      表单/画布编辑实时回写 YAML（注释与排版会被规范化）
                    </span>
                  </div>
                  {editorMode === 'form' ? (
                    <Suspense fallback={<p className="text-xs text-ink-muted">加载表单…</p>}>
                      <PipelineForm yamlText={draft} adapters={adapterNames} onChange={setDraft} />
                    </Suspense>
                  ) : editorMode === 'canvas' ? (
                    <Suspense fallback={<p className="text-xs text-ink-muted">加载画布…</p>}>
                      <PipelineCanvasEditor
                        yamlText={draft}
                        adapters={adapterNames}
                        onChange={setDraft}
                        onSwitchToForm={() => setEditorMode('form')}
                      />
                    </Suspense>
                  ) : (
                    <textarea
                      aria-label="管道 YAML"
                      value={draft}
                      onChange={(event) => setDraft(event.target.value)}
                      rows={18}
                      spellCheck={false}
                      className="w-full rounded-card border border-surface-border bg-surface p-3 font-mono text-xs"
                    />
                  )}
                </div>
              ) : (
                <pre className="max-h-96 overflow-auto rounded-card bg-surface-muted p-3 font-mono text-xs">
                  {detail.yaml}
                </pre>
              )}

              <details>
                <summary className="cursor-pointer text-xs text-ink-muted">
                  执行计划（冻结进任务快照的路由结构）
                </summary>
                <div className="mt-2">
                  <button
                    type="button"
                    aria-expanded={canvasOpen}
                    onClick={() => setCanvasOpen((prev) => !prev)}
                    className="rounded-tag border border-surface-border px-2 py-0.5 text-xs hover:bg-surface-muted"
                  >
                    {canvasOpen ? '隐藏拓扑画布' : '拓扑画布'}
                  </button>
                  {canvasOpen && (
                    <div className="mt-2">
                      <Suspense fallback={<p className="text-xs text-ink-muted">加载画布…</p>}>
                        <PipelineCanvas plan={detail.execution_plan} />
                      </Suspense>
                    </div>
                  )}
                </div>
                <pre className="mt-2 max-h-64 overflow-auto rounded-card bg-surface-muted p-3 font-mono text-xs">
                  {JSON.stringify(detail.execution_plan, null, 2)}
                </pre>
              </details>

              <details className="rounded-card border border-surface-border p-3 text-xs">
                <summary className="cursor-pointer text-ink-muted">
                  路由解释（dry-run：只预检与降级链解释，不执行解析）
                </summary>
                <div className="mt-2 flex flex-wrap items-end gap-2">
                  <label className="space-y-1">
                    <span className="block text-ink-muted">样本文档版本</span>
                    <select
                      aria-label="dry-run 样本文档"
                      value={sampleDocId}
                      onChange={(event) => setSampleDocId(event.target.value)}
                      className="rounded-btn border border-surface-border bg-surface px-2 py-1"
                    >
                      <option value="">选择文档</option>
                      {documents.map((document) => (
                        <option key={document.id} value={document.id}>
                          {document.filename}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    type="button"
                    disabled={!sampleDocId || dryRunning}
                    onClick={() => {
                      const sample = documents.find((item) => item.id === sampleDocId);
                      if (!sample) return;
                      onDryRun({
                        name: detail.name,
                        yamlText: editing ? draft : detail.yaml,
                        versionId: sample.versionId,
                      });
                    }}
                    className="rounded-btn border border-surface-border px-3 py-1 hover:bg-surface-muted disabled:opacity-50"
                  >
                    {dryRunning ? '解释中…' : '解释路由'}
                  </button>
                  <span className="text-ink-muted">使用当前编辑器内容（未保存草稿也可）</span>
                </div>
                {documents.length === 0 && (
                  <p className="mt-2 text-ink-muted">暂无可用于 dry-run 的文档版本。</p>
                )}
                {dryRunError && (
                  <p role="alert" data-testid="dry-run-error" className="mt-2 text-state-danger">
                    {dryRunError}
                  </p>
                )}
                {dryRun && <DryRunView result={dryRun} />}
              </details>

              <div className="rounded-card border border-surface-border p-3 text-xs">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-ink-muted">
                    A/B 实验（按文档稳定分桶，变体冻结进任务快照）
                  </span>
                  {!experimentOpen && (
                    <button
                      type="button"
                      onClick={() => {
                        const currentYaml = editing ? draft : detail.yaml;
                        setExpName(experiment?.name ?? `${detail.name}-ab`);
                        setExpTraffic(experiment?.traffic_percent ?? 50);
                        setExpActive(experiment?.active ?? true);
                        setExpAYaml(experiment?.variant_a_yaml ?? currentYaml);
                        setExpBYaml(experiment?.variant_b_yaml ?? currentYaml);
                        setExperimentOpen(true);
                      }}
                      className="rounded-tag border border-surface-border px-2 py-0.5 hover:bg-surface-muted"
                    >
                      {experiment ? '编辑实验' : '配置实验'}
                    </button>
                  )}
                </div>
                {experimentPending && <p className="mt-2 text-ink-muted">加载实验…</p>}
                {experiment && (
                  <div data-testid="pipeline-experiment" className="mt-2 space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{experiment.name}</span>
                      <span className="rounded-tag bg-surface-muted px-2 py-0.5 tabular-nums">
                        rev {experiment.revision}
                      </span>
                      <span
                        className={`rounded-tag px-2 py-0.5 ${
                          experiment.active
                            ? 'bg-brand-soft text-brand'
                            : 'bg-surface-muted text-ink-muted'
                        }`}
                      >
                        {experiment.active ? '运行中' : '已暂停'}
                      </span>
                      <span className="tabular-nums">B 组 {experiment.traffic_percent}%</span>
                    </div>
                    <div className="text-ink-muted">
                      digest A {shortDigest(experiment.variant_a_digest)} · B{' '}
                      {shortDigest(experiment.variant_b_digest)}
                    </div>
                    {experimentReportPending && (
                      <p className="text-ink-muted">加载对照…</p>
                    )}
                    {experimentReport && (
                      <>
                        <table data-testid="experiment-report" className="mt-1 w-full text-xs">
                          <thead>
                            <tr className="border-b border-surface-border text-left text-ink-muted">
                              <th className="px-2 py-1">组</th>
                              <th className="px-2 py-1">任务</th>
                              <th className="px-2 py-1">完成</th>
                              <th className="px-2 py-1">失败</th>
                              <th className="px-2 py-1">进行中</th>
                              <th className="px-2 py-1">平均覆盖</th>
                            </tr>
                          </thead>
                          <tbody>
                            {(['A', 'B'] as const).map((variant) => {
                              const stats = experimentReport.variants[variant];
                              return (
                                <tr
                                  key={variant}
                                  className="border-b border-surface-border last:border-b-0"
                                >
                                  <td className="px-2 py-1 font-medium">{variant}</td>
                                  <td className="px-2 py-1 tabular-nums">{stats.jobs}</td>
                                  <td className="px-2 py-1 tabular-nums">{stats.done}</td>
                                  <td className="px-2 py-1 tabular-nums">{stats.failed}</td>
                                  <td className="px-2 py-1 tabular-nums">{stats.in_progress}</td>
                                  <td className="px-2 py-1 tabular-nums">
                                    {stats.avg_coverage !== null
                                      ? `${(stats.avg_coverage * 100).toFixed(1)}%`
                                      : '—'}
                                  </td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                        {qualityKeys.length > 0 && (
                          <div className="mt-2">
                            <div className="text-ink-muted">
                              质量指标（黄金集回归按候选版本归因；仅比例类指标）
                            </div>
                            <table
                              data-testid="experiment-quality"
                              className="mt-1 w-full text-xs"
                            >
                              <thead>
                                <tr className="border-b border-surface-border text-left text-ink-muted">
                                  <th className="px-2 py-1">组</th>
                                  <th className="px-2 py-1">对比用例</th>
                                  {qualityKeys.map((metric) => (
                                    <th key={metric} className="px-2 py-1">
                                      {metric}
                                    </th>
                                  ))}
                                </tr>
                              </thead>
                              <tbody>
                                {(['A', 'B'] as const).map((variant) => {
                                  const stats = experimentReport.variants[variant];
                                  return (
                                    <tr
                                      key={variant}
                                      className="border-b border-surface-border last:border-b-0"
                                    >
                                      <td className="px-2 py-1 font-medium">{variant}</td>
                                      <td className="px-2 py-1 tabular-nums">
                                        {stats.quality_cases}
                                      </td>
                                      {qualityKeys.map((metric) => {
                                        const value = stats.quality_metrics[metric];
                                        return (
                                          <td key={metric} className="px-2 py-1 tabular-nums">
                                            {value !== undefined ? value.toFixed(4) : '—'}
                                          </td>
                                        );
                                      })}
                                    </tr>
                                  );
                                })}
                              </tbody>
                            </table>
                          </div>
                        )}
                      </>
                    )}
                  </div>
                )}
                {experimentOpen && (
                  <div className="mt-2 space-y-2">
                    <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
                      <label className="space-y-1">
                        <span className="block text-ink-muted">实验名称</span>
                        <input
                          aria-label="实验名称"
                          value={expName}
                          onChange={(event) => setExpName(event.target.value)}
                          className="w-full rounded-btn border border-surface-border bg-surface px-2 py-1"
                        />
                      </label>
                      <label className="space-y-1">
                        <span className="block text-ink-muted">B 组流量（0-100）</span>
                        <input
                          aria-label="B 组流量"
                          type="number"
                          min={0}
                          max={100}
                          value={expTraffic}
                          onChange={(event) =>
                            setExpTraffic(
                              Math.max(0, Math.min(100, Number(event.target.value) || 0)),
                            )
                          }
                          className="w-full rounded-btn border border-surface-border bg-surface px-2 py-1"
                        />
                      </label>
                      <label className="flex items-center gap-1 pt-4 text-ink-muted">
                        <input
                          type="checkbox"
                          checked={expActive}
                          onChange={(event) => setExpActive(event.target.checked)}
                        />
                        运行中
                      </label>
                    </div>
                    <label className="block space-y-1">
                      <span className="block text-ink-muted">变体 A YAML</span>
                      <textarea
                        aria-label="变体 A YAML"
                        value={expAYaml}
                        onChange={(event) => setExpAYaml(event.target.value)}
                        rows={8}
                        spellCheck={false}
                        className="w-full rounded-card border border-surface-border bg-surface p-2 font-mono text-xs"
                      />
                    </label>
                    <label className="block space-y-1">
                      <span className="block text-ink-muted">变体 B YAML</span>
                      <textarea
                        aria-label="变体 B YAML"
                        value={expBYaml}
                        onChange={(event) => setExpBYaml(event.target.value)}
                        rows={8}
                        spellCheck={false}
                        className="w-full rounded-card border border-surface-border bg-surface p-2 font-mono text-xs"
                      />
                    </label>
                    <div className="flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        disabled={experimentSaving || !expName.trim()}
                        onClick={() =>
                          onSaveExperiment({
                            pipelineName: detail.name,
                            name: expName.trim(),
                            variantAYaml: expAYaml,
                            variantBYaml: expBYaml,
                            trafficPercent: expTraffic,
                            active: expActive,
                          })
                        }
                        className="rounded-btn bg-brand px-3 py-1 text-xs text-white disabled:opacity-50"
                      >
                        {experimentSaving ? '保存中…' : '保存实验'}
                      </button>
                      <button
                        type="button"
                        onClick={() => setExperimentOpen(false)}
                        className="rounded-btn border border-surface-border px-3 py-1 text-xs"
                      >
                        收起
                      </button>
                      <span className="text-ink-muted">
                        变体 YAML 的 name 需与 {detail.name} 一致
                      </span>
                    </div>
                    {experimentError && (
                      <p role="alert" data-testid="experiment-error" className="text-state-danger">
                        {experimentError}
                      </p>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

export function PipelinesPage() {
  const tenantId = useSessionStore((s) => s.currentTenantId);
  const csrfToken = useSessionStore((s) => s.csrfToken);
  const isPlatformAdmin = useSessionStore((s) => Boolean(s.user?.is_platform_admin));
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<BoardProps['saveState']>(null);
  const [validation, setValidation] = useState<PipelineValidationResult | null>(null);
  const [dryRun, setDryRun] = useState<PipelineDryRunResult | null>(null);
  const [dryRunError, setDryRunError] = useState<string | null>(null);
  const [experimentError, setExperimentError] = useState<string | null>(null);
  const [adapterActionError, setAdapterActionError] = useState<string | null>(null);

  const list = useQuery({
    queryKey: ['pipelines', tenantId],
    enabled: Boolean(tenantId),
    queryFn: () => pipelines(tenantId ?? ''),
  });

  const detail = useQuery({
    queryKey: ['pipeline', tenantId, selected],
    enabled: Boolean(tenantId && selected),
    queryFn: () => pipelineDetail(selected ?? '', tenantId ?? ''),
  });

  const documents = useQuery({
    queryKey: ['documents', tenantId],
    enabled: Boolean(tenantId),
    queryFn: () => request<ListPage<DocumentItem>>('/documents?limit=100', { tenantId }),
  });

  const adapterList = useQuery({
    queryKey: ['adapters', tenantId],
    enabled: Boolean(tenantId),
    queryFn: () => fetchAdapters(tenantId ?? ''),
  });

  const registerAdapterMutation = useMutation({
    mutationFn: (values: AdapterRegisterInput) =>
      registerAdapter({ ...values, tenantId: tenantId ?? '', csrfToken }),
    onSuccess: () => {
      setAdapterActionError(null);
      void queryClient.invalidateQueries({ queryKey: ['adapters', tenantId] });
    },
    onError: (err) =>
      setAdapterActionError(err instanceof ApiError ? err.message : 'adapter 注册失败'),
  });

  const adapterHeartbeatMutation = useMutation({
    mutationFn: (name: string) =>
      adapterHeartbeat({ name, health: 'ready', tenantId: tenantId ?? '', csrfToken }),
    onSuccess: () => {
      setAdapterActionError(null);
      void queryClient.invalidateQueries({ queryKey: ['adapters', tenantId] });
    },
    onError: (err) =>
      setAdapterActionError(err instanceof ApiError ? err.message : 'adapter 心跳失败'),
  });

  const experiment = useQuery({
    queryKey: ['pipeline-experiment', tenantId, selected],
    enabled: Boolean(tenantId && selected),
    queryFn: () => pipelineExperiment(selected ?? '', tenantId ?? ''),
  });

  const experimentReport = useQuery({
    queryKey: ['pipeline-experiment-report', tenantId, selected],
    enabled: Boolean(tenantId && selected && experiment.data),
    queryFn: () => pipelineExperimentReport(selected ?? '', tenantId ?? ''),
  });

  const save = useMutation({
    mutationFn: (payload: { name: string; yamlText: string; expectedVersion: number }) =>
      savePipeline({ ...payload, tenantId: tenantId ?? '', csrfToken }),
    onSuccess: (result, payload) => {
      setSaveState({
        kind: 'ok',
        message: `已保存为新版本 v${result.version}（digest ${result.digest.slice(0, 12)}…）`,
      });
      setValidation(null);
      setDryRun(null);
      void queryClient.invalidateQueries({ queryKey: ['pipelines', tenantId] });
      void queryClient.invalidateQueries({ queryKey: ['pipeline', tenantId, payload.name] });
    },
    onError: (err) => {
      const message =
        err instanceof ApiError
          ? err.status === 412
            ? '版本冲突（412）：配置已被其他操作修改，请刷新后重试'
            : err.message
          : '保存失败';
      setSaveState({ kind: 'error', message });
    },
  });

  const validate = useMutation({
    mutationFn: (payload: { name: string; yamlText: string }) =>
      validatePipeline({ ...payload, tenantId: tenantId ?? '', csrfToken }),
    onSuccess: (result) => setValidation(result),
    onError: (err) => {
      setValidation({
        ok: false,
        errors: [err instanceof ApiError ? err.message : '校验请求失败'],
      });
    },
  });

  const explain = useMutation({
    mutationFn: (payload: { name: string; yamlText: string; versionId: string }) =>
      pipelineDryRun({ ...payload, tenantId: tenantId ?? '', csrfToken }),
    onSuccess: (result) => {
      setDryRun(result);
      setDryRunError(null);
    },
    onError: (err) => {
      setDryRun(null);
      setDryRunError(err instanceof ApiError ? err.message : 'dry-run 请求失败');
    },
  });

  const saveExperiment = useMutation({
    mutationFn: (payload: {
      pipelineName: string;
      name: string;
      variantAYaml: string;
      variantBYaml: string;
      trafficPercent: number;
      active: boolean;
    }) => savePipelineExperiment({ ...payload, tenantId: tenantId ?? '', csrfToken }),
    onSuccess: (result) => {
      setExperimentError(null);
      queryClient.setQueryData(['pipeline-experiment', tenantId, selected], result);
      void queryClient.invalidateQueries({
        queryKey: ['pipeline-experiment-report', tenantId, selected],
      });
    },
    onError: (err) => {
      setExperimentError(err instanceof ApiError ? err.message : '实验保存失败');
    },
  });

  if (list.isPending) return <SkeletonRows />;

  const samples = (documents.data?.items ?? [])
    .map((item) => ({
      id: item.id,
      filename: item.filename,
      versionId: item.visible_version_id ?? item.desired_version_id,
    }))
    .filter(
      (item): item is { id: string; filename: string; versionId: string } => Boolean(item.versionId),
    );

  return (
    <PipelinesBoard
      items={list.data ?? []}
      detail={detail.data ?? null}
      detailPending={detail.isPending && selected !== null}
      documents={samples}
      adapterNames={(adapterList.data ?? []).map((item) => item.name)}
      adapterInfo={adapterList.data ?? []}
      isPlatformAdmin={isPlatformAdmin}
      adapterRegistering={registerAdapterMutation.isPending}
      adapterHeartbeatName={adapterHeartbeatMutation.variables ?? null}
      adapterActionError={adapterActionError}
      onRegisterAdapter={(values) => registerAdapterMutation.mutate(values)}
      onAdapterHeartbeat={(name) => adapterHeartbeatMutation.mutate(name)}
      onSelect={(name) => {
        setSelected(name);
        setSaveState(null);
        setValidation(null);
        setDryRun(null);
        setDryRunError(null);
        setExperimentError(null);
      }}
      onSave={(name, yamlText, expectedVersion) => save.mutate({ name, yamlText, expectedVersion })}
      saving={save.isPending}
      saveState={saveState}
      listError={
        list.isError ? (list.error instanceof ApiError ? list.error : new Error('加载失败')) : null
      }
      onValidate={(name, yamlText) => validate.mutate({ name, yamlText })}
      validating={validate.isPending}
      validation={validation}
      onDryRun={(payload) => explain.mutate(payload)}
      dryRunning={explain.isPending}
      dryRun={dryRun}
      dryRunError={dryRunError}
      experiment={experiment.data ?? null}
      experimentPending={experiment.isPending && selected !== null}
      experimentSaving={saveExperiment.isPending}
      experimentError={experimentError}
      experimentReport={experimentReport.data ?? null}
      experimentReportPending={experimentReport.isPending && Boolean(experiment.data)}
      onSaveExperiment={(payload) => saveExperiment.mutate(payload)}
    />
  );
}
