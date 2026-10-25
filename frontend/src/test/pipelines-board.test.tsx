/** 解析管道页（纯组件）测试：列表/详情、编辑保存（If-Match 版本）、错误与提示。 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type {
  PipelineDetail,
  PipelineDryRunResult,
  PipelineExperiment,
  PipelineExperimentReport,
  PipelineSummary,
} from '../api/client';
import { PipelinesBoard } from '../pages/PipelinesPage';

const ITEMS: PipelineSummary[] = [
  { name: 'default', version: 1, active: true, digest: 'd'.repeat(64), source: 'builtin' },
  { name: 'custom-fast', version: 2, active: true, digest: 'c'.repeat(64), source: 'tenant' },
];

const DETAIL: PipelineDetail = {
  name: 'custom-fast',
  version: 2,
  active: true,
  digest: 'c'.repeat(64),
  source: 'tenant',
  yaml: 'schema_version: "1.0"\nname: custom-fast\nrevision: 2\npipelines:\n  pdf:\n    mode: first_success\n    candidates:\n      - adapter: pdf-text-local\n',
  execution_plan: {
    pipeline_name: 'custom-fast',
    pipeline_revision: 2,
    config_digest: 'c'.repeat(64),
    pipelines: {
      pdf: {
        mode: 'first_success',
        steps: [{ adapter: 'pdf-text-local', required: true }],
      },
    },
  },
};

const DRY_RUN: PipelineDryRunResult = {
  ok: true,
  pipeline: { name: 'custom-fast', version: 2, digest: 'c'.repeat(64), source: 'draft' },
  format: 'pdf',
  features: {
    mime: 'application/pdf',
    extension: 'pdf',
    size_bytes: 1024,
    text_layer_ratio: 0.3,
    page_count: 2,
    encrypted: false,
    has_zip_container: false,
    language_hint: null,
    extra: {},
  },
  route: {
    mode: 'first_success',
    adapter: 'pdf-text-local',
    reason: '候选 pdf-text-local 命中',
    skipped: [{ adapter: 'ocr-local', reason: 'when 条件不命中' }],
  },
  warnings: ['文本层覆盖率低于 0.5：默认分支要求 text_layer_ratio>=0.5（OCR 未启用）'],
};

const DOCUMENTS = [{ id: 'doc-1', filename: 'sample.pdf', versionId: 'ver-1' }];

const EXPERIMENT: PipelineExperiment = {
  id: 'exp-1',
  pipeline_name: 'custom-fast',
  name: 'ab-1',
  traffic_percent: 25,
  active: true,
  revision: 3,
  variant_a_digest: 'a'.repeat(64),
  variant_b_digest: 'b'.repeat(64),
  variant_a_yaml: 'schema_version: "1.0"\nname: custom-fast\nrevision: 3\npipelines: {}\n',
  variant_b_yaml: 'schema_version: "1.0"\nname: custom-fast\nrevision: 4\npipelines: {}\n',
  created_at: '2026-10-17T10:00:00Z',
  updated_at: '2026-10-17T10:00:00Z',
};

const EXPERIMENT_REPORT: PipelineExperimentReport = {
  experiment: {
    id: 'exp-1',
    pipeline_name: 'custom-fast',
    name: 'ab-1',
    revision: 3,
    traffic_percent: 25,
    active: true,
  },
  total_jobs: 5,
  scanned_jobs: 5,
  variants: {
    A: {
      jobs: 4,
      done: 3,
      partial_done: 0,
      failed: 1,
      cancelled: 0,
      in_progress: 0,
      avg_coverage: 0.975,
      avg_elapsed_seconds: 12.5,
      quality_cases: 2,
      quality_metrics: { reading_order: 0.91, text_cer: 0.0312 },
    },
    B: {
      jobs: 1,
      done: 1,
      partial_done: 0,
      failed: 0,
      cancelled: 0,
      in_progress: 0,
      avg_coverage: 1,
      avg_elapsed_seconds: 10,
      quality_cases: 1,
      quality_metrics: { text_cer: 0.02 },
    },
  },
};

function renderBoard(overrides: Partial<Parameters<typeof PipelinesBoard>[0]> = {}) {
  return render(
    <PipelinesBoard
      items={ITEMS}
      detail={null}
      detailPending={false}
      documents={DOCUMENTS}
      onSelect={() => undefined}
      onSave={() => undefined}
      saving={false}
      saveState={null}
      listError={null}
      onValidate={() => undefined}
      validating={false}
      validation={null}
      onDryRun={() => undefined}
      dryRunning={false}
      dryRun={null}
      dryRunError={null}
      experiment={null}
      experimentPending={false}
      experimentSaving={false}
      experimentError={null}
      onSaveExperiment={() => undefined}
      {...overrides}
    />,
  );
}

describe('解析管道页', () => {
  it('列表区分内置与租户覆盖，展示版本与 digest 短串', () => {
    renderBoard();
    expect(screen.getByText('内置')).toBeInTheDocument();
    expect(screen.getByText('租户覆盖')).toBeInTheDocument();
    expect(screen.getByText('v2')).toBeInTheDocument();
    expect(screen.getByText(/digest cccccccccccc…/)).toBeInTheDocument();
  });

  it('选择管道回调传出 name', () => {
    const onSelect = vi.fn();
    renderBoard({ onSelect });
    fireEvent.click(screen.getByRole('button', { name: /custom-fast/ }));
    expect(onSelect).toHaveBeenCalledWith('custom-fast');
  });

  it('编辑保存：回调携带当前版本作为 If-Match；取消不触发保存', () => {
    const onSave = vi.fn();
    renderBoard({ detail: DETAIL, onSave });
    fireEvent.click(screen.getByRole('button', { name: '编辑' }));

    const editor = screen.getByLabelText('管道 YAML');
    expect(editor).toHaveValue(DETAIL.yaml);
    fireEvent.change(editor, { target: { value: `${DETAIL.yaml}# tuned\n` } });
    fireEvent.click(screen.getByRole('button', { name: /保存为新版本（If-Match v2）/ }));
    expect(onSave).toHaveBeenCalledWith('custom-fast', `${DETAIL.yaml}# tuned\n`, 2);

    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(screen.queryByLabelText('管道 YAML')).not.toBeInTheDocument();
  });

  it('表单模式：结构编辑回写 YAML，切回 YAML 可见序列化结果', async () => {
    renderBoard({ detail: DETAIL, adapterNames: ['pdf-text-local'] });
    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    fireEvent.click(screen.getByRole('button', { name: '表单' }));
    expect(await screen.findByTestId('pipeline-form')).toBeInTheDocument();
    expect(screen.getByLabelText('pdf 步骤 1 adapter')).toHaveValue('pdf-text-local');

    fireEvent.change(screen.getByLabelText('pdf 模式'), { target: { value: 'sequence' } });
    fireEvent.click(screen.getByRole('button', { name: 'YAML' }));
    const editor = screen.getByLabelText('管道 YAML') as HTMLTextAreaElement;
    expect(editor.value).toContain('mode: sequence');
    expect(editor.value).toContain('adapter: pdf-text-local');
    expect(editor.value).not.toContain('candidates');
  });

  it('编辑态可切换画布并通过＋节点追加步骤回写 YAML', async () => {
    renderBoard({ detail: DETAIL, adapterNames: ['pdf-text-local'] });
    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    fireEvent.click(screen.getByRole('button', { name: '画布' }));
    // 画布经 React.lazy 加载；全量并行跑测试时动态 import 可能超过 findBy 默认的 1 秒。
    expect(
      await screen.findByTestId('pipeline-canvas-editor', {}, { timeout: 5_000 }),
    ).toBeInTheDocument();

    const addNode = document.querySelector('[data-id="add:pdf"]');
    expect(addNode).not.toBeNull();
    fireEvent.click(addNode!);

    fireEvent.click(screen.getByRole('button', { name: 'YAML' }));
    const editor = screen.getByLabelText('管道 YAML') as HTMLTextAreaElement;
    expect(editor.value.match(/- adapter:/g)?.length).toBe(2);
  });

  it('草稿 YAML 无法解析时表单模式展示错误提示', async () => {
    renderBoard({ detail: DETAIL, adapterNames: [] });
    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    fireEvent.change(screen.getByLabelText('管道 YAML'), {
      target: { value: 'pipelines:\n  pdf: [\n' },
    });
    fireEvent.click(screen.getByRole('button', { name: '表单' }));
    expect(await screen.findByTestId('pipeline-form-error')).toBeInTheDocument();
  });

  it('保存中禁用按钮；错误态以 alert 展示（如 412 冲突）', () => {
    const { unmount } = renderBoard({ detail: DETAIL, saving: true });
    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    expect(screen.getByRole('button', { name: '保存中…' })).toBeDisabled();
    unmount();

    renderBoard({
      detail: DETAIL,
      saveState: { kind: 'error', message: '版本冲突（412）：配置已被其他操作修改，请刷新后重试' },
    });
    expect(screen.getByRole('alert')).toHaveTextContent('412');
  });

  it('执行计划折叠区展示冻结路由结构', () => {
    renderBoard({ detail: DETAIL });
    expect(screen.getByText('执行计划（冻结进任务快照的路由结构）')).toBeInTheDocument();
    expect(screen.getByText(/"pipeline_name": "custom-fast"/)).toBeInTheDocument();
  });

  it('执行计划可切换只读拓扑画布', async () => {
    renderBoard({ detail: DETAIL });
    expect(screen.queryByTestId('pipeline-canvas')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '拓扑画布' }));
    expect(await screen.findByTestId('pipeline-canvas')).toBeInTheDocument();
    expect(screen.getByText('pdf-text-local（必需）')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '隐藏拓扑画布' }));
    expect(screen.queryByTestId('pipeline-canvas')).not.toBeInTheDocument();
  });

  it('校验草稿：回调携带当前编辑器内容，通过时展示版本与 digest', () => {
    const onValidate = vi.fn();
    renderBoard({
      detail: DETAIL,
      onValidate,
      validation: { ok: true, errors: [], version: 2, digest: 'c'.repeat(64) },
    });
    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    const editor = screen.getByLabelText('管道 YAML');
    fireEvent.change(editor, { target: { value: `${DETAIL.yaml}# draft\n` } });
    fireEvent.click(screen.getByRole('button', { name: '校验草稿' }));

    expect(onValidate).toHaveBeenCalledWith('custom-fast', `${DETAIL.yaml}# draft\n`);
    expect(screen.getByTestId('pipeline-validation')).toHaveTextContent('草稿校验通过');
    expect(screen.getByTestId('pipeline-validation')).toHaveTextContent('digest cccccccccccc…');
  });

  it('校验失败：逐条展示服务端错误（如未注册 adapter）', () => {
    renderBoard({
      detail: DETAIL,
      validation: { ok: false, errors: ['pipeline pdf 引用了未注册 adapter: missing'] },
    });
    expect(screen.getByTestId('pipeline-validation')).toHaveTextContent('未注册 adapter');
    expect(screen.getByRole('alert')).toHaveTextContent('pipeline pdf');
  });

  it('dry-run：选择样本文档后回调携带名称/当前内容/版本', () => {
    const onDryRun = vi.fn();
    renderBoard({ detail: DETAIL, onDryRun });
    const runButton = screen.getByRole('button', { name: '解释路由' });
    expect(runButton).toBeDisabled();

    fireEvent.change(screen.getByLabelText('dry-run 样本文档'), { target: { value: 'doc-1' } });
    expect(runButton).not.toBeDisabled();
    fireEvent.click(runButton);
    expect(onDryRun).toHaveBeenCalledWith({
      name: 'custom-fast',
      yamlText: DETAIL.yaml,
      versionId: 'ver-1',
    });
  });

  it('dry-run 使用未保存草稿（编辑态内容优先）', () => {
    const onDryRun = vi.fn();
    renderBoard({ detail: DETAIL, onDryRun });
    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    fireEvent.change(screen.getByLabelText('管道 YAML'), {
      target: { value: `${DETAIL.yaml}# unsaved\n` },
    });
    fireEvent.change(screen.getByLabelText('dry-run 样本文档'), { target: { value: 'doc-1' } });
    fireEvent.click(screen.getByRole('button', { name: '解释路由' }));
    expect(onDryRun).toHaveBeenCalledWith({
      name: 'custom-fast',
      yamlText: `${DETAIL.yaml}# unsaved\n`,
      versionId: 'ver-1',
    });
  });

  it('dry-run 结果：格式/命中 adapter/降级链/特征/警告', () => {
    renderBoard({ detail: DETAIL, dryRun: DRY_RUN });
    const result = screen.getByTestId('pipeline-dry-run');
    expect(result).toHaveTextContent('pdf');
    expect(result).toHaveTextContent('pipeline custom-fast v2（草稿）');
    expect(screen.getByTestId('dry-run-adapter')).toHaveTextContent('pdf-text-local');
    expect(screen.getByTestId('dry-run-skipped')).toHaveTextContent('ocr-local');
    expect(screen.getByTestId('dry-run-warnings')).toHaveTextContent('文本层覆盖率低于');
    expect(result).toHaveTextContent('页数=2');
    expect(result).toHaveTextContent('文本层=0.30');
  });

  it('dry-run 无可用分支：明确提示且展示原因；无样本文档时禁用按钮', () => {
    renderBoard({
      detail: DETAIL,
      documents: [],
      dryRun: {
        ...DRY_RUN,
        ok: false,
        format: 'bin',
        route: {
          ...DRY_RUN.route,
          adapter: null,
          reason: '没有针对格式 bin 的 pipeline 分支',
          skipped: [],
        },
        warnings: ['pipeline 未定义格式 bin 的分支'],
      },
    });
    expect(screen.getByTestId('dry-run-no-route')).toBeInTheDocument();
    expect(screen.getByTestId('dry-run-reason')).toHaveTextContent('没有针对格式 bin');
    expect(screen.getByText('暂无可用于 dry-run 的文档版本。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '解释路由' })).toBeDisabled();
  });

  it('dry-run 请求失败以 alert 展示', () => {
    renderBoard({ detail: DETAIL, dryRunError: '文档版本不存在' });
    expect(screen.getByTestId('dry-run-error')).toHaveTextContent('文档版本不存在');
  });

  it('配置实验：预填当前 YAML，保存回调携带完整 payload', () => {
    const onSaveExperiment = vi.fn();
    renderBoard({ detail: DETAIL, onSaveExperiment });
    fireEvent.click(screen.getByRole('button', { name: '配置实验' }));

    expect(screen.getByLabelText('实验名称')).toHaveValue('custom-fast-ab');
    expect(screen.getByLabelText('变体 A YAML')).toHaveValue(DETAIL.yaml);
    expect(screen.getByLabelText('变体 B YAML')).toHaveValue(DETAIL.yaml);
    expect(screen.getByLabelText('B 组流量')).toHaveValue(50);

    fireEvent.change(screen.getByLabelText('实验名称'), { target: { value: 'ab-tuning' } });
    fireEvent.change(screen.getByLabelText('B 组流量'), { target: { value: '30' } });
    fireEvent.click(screen.getByRole('button', { name: '保存实验' }));

    expect(onSaveExperiment).toHaveBeenCalledWith({
      pipelineName: 'custom-fast',
      name: 'ab-tuning',
      variantAYaml: DETAIL.yaml,
      variantBYaml: DETAIL.yaml,
      trafficPercent: 30,
      active: true,
    });
  });

  it('已有实验：展示 revision/流量/状态，编辑时预填变体 YAML', () => {
    renderBoard({ detail: DETAIL, experiment: EXPERIMENT });
    const summary = screen.getByTestId('pipeline-experiment');
    expect(summary).toHaveTextContent('rev 3');
    expect(summary).toHaveTextContent('B 组 25%');
    expect(summary).toHaveTextContent('运行中');

    fireEvent.click(screen.getByRole('button', { name: '编辑实验' }));
    expect(screen.getByLabelText('实验名称')).toHaveValue('ab-1');
    expect(screen.getByLabelText('B 组流量')).toHaveValue(25);
    expect(screen.getByLabelText('变体 B YAML')).toHaveValue(EXPERIMENT.variant_b_yaml);
  });

  it('实验保存中禁用按钮；失败以 alert 展示', () => {
    renderBoard({ detail: DETAIL, experimentSaving: true, experimentError: '变体 B 校验失败' });
    fireEvent.click(screen.getByRole('button', { name: '配置实验' }));
    expect(screen.getByRole('button', { name: '保存中…' })).toBeDisabled();
    expect(screen.getByTestId('experiment-error')).toHaveTextContent('变体 B 校验失败');
  });

  it('A/B 对照表：按 variant 展示任务/完成/失败/平均覆盖率', () => {
    renderBoard({
      detail: DETAIL,
      experiment: EXPERIMENT,
      experimentReport: EXPERIMENT_REPORT,
    });
    const table = screen.getByTestId('experiment-report');
    const rows = within(table).getAllByRole('row');
    expect(rows).toHaveLength(3); // 表头 + A + B
    expect(rows[1]).toHaveTextContent('A');
    expect(rows[1]).toHaveTextContent('4');
    expect(rows[1]).toHaveTextContent('3');
    expect(rows[1]).toHaveTextContent('1');
    expect(rows[1]).toHaveTextContent('97.5%');
    expect(rows[2]).toHaveTextContent('B');
    expect(rows[2]).toHaveTextContent('100.0%');
  });

  it('质量指标表：展示归因用例数与 A/B 并集指标（缺失显示 —）', () => {
    renderBoard({
      detail: DETAIL,
      experiment: EXPERIMENT,
      experimentReport: EXPERIMENT_REPORT,
    });
    const table = screen.getByTestId('experiment-quality');
    const rows = within(table).getAllByRole('row');
    expect(rows).toHaveLength(3); // 表头 + A + B
    expect(rows[0]).toHaveTextContent('text_cer');
    expect(rows[0]).toHaveTextContent('reading_order');
    expect(rows[1]).toHaveTextContent('2');
    expect(rows[1]).toHaveTextContent('0.0312');
    expect(rows[1]).toHaveTextContent('0.9100');
    expect(rows[2]).toHaveTextContent('1');
    expect(rows[2]).toHaveTextContent('0.0200');
    expect(rows[2]).toHaveTextContent('—'); // B 无 reading_order 样本
  });

  it('无质量样本时隐藏质量表', () => {
    renderBoard({
      detail: DETAIL,
      experiment: EXPERIMENT,
      experimentReport: {
        ...EXPERIMENT_REPORT,
        variants: {
          A: { ...EXPERIMENT_REPORT.variants.A, quality_cases: 0, quality_metrics: {} },
          B: { ...EXPERIMENT_REPORT.variants.B, quality_cases: 0, quality_metrics: {} },
        },
      },
    });
    expect(screen.queryByTestId('experiment-quality')).not.toBeInTheDocument();
  });

  it('对照加载中如实提示', () => {
    renderBoard({
      detail: DETAIL,
      experiment: EXPERIMENT,
      experimentReportPending: true,
    });
    expect(screen.getByText('加载对照…')).toBeInTheDocument();
  });
});
