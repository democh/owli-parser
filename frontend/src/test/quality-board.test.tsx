/**
 * 质量回归面板（纯组件 + 指标 diff 纯函数）测试。
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { RegressionRun, RegressionRunDetail } from '../api/client';
import {
  QualityBoard,
  casesToCsv,
  deriveGoldenSets,
  diffRegressionMetrics,
  metricsDiffToCsv,
} from '../pages/QualityPage';

const RUN_A: RegressionRun = {
  id: 'run-a',
  golden_set_id: 'aaaaaaaa-1111-2222-3333-444444444444',
  golden_set_name: 'retrieval-smoke',
  run_id: 'a1b2c3d4e5f6',
  adapter_ref: 'lexical-bm25',
  config_digest: 'cfg-v1',
  state: 'passed',
  created_at: '2026-10-15T09:00:00+08:00',
  aggregate: { 'recall@3': 1.0, mrr: 1.0, 'ndcg@5': 0.9 },
  passed: true,
  violations: [],
};

const RUN_B: RegressionRun = {
  ...RUN_A,
  id: 'run-b',
  run_id: 'f6e5d4c3b2a1',
  state: 'failed',
  created_at: '2026-10-15T10:00:00+08:00',
  aggregate: { 'recall@3': 0.8, mrr: 0.7, 'precision@3': 0.5 },
  passed: false,
  violations: ['recall@3 聚合 0.8000 < 期望 0.9000（阈值 recall@3）'],
};

describe('diffRegressionMetrics（指标差异纯函数）', () => {
  it('共有指标算 delta；仅单侧存在的记 null（不伪造 0）', () => {
    const rows = diffRegressionMetrics(RUN_A.aggregate, RUN_B.aggregate);
    const byName = Object.fromEntries(rows.map((row) => [row.metric, row]));
    expect(byName['recall@3']).toMatchObject({ baseline: 1.0, candidate: 0.8 });
    expect(byName['recall@3']?.delta).toBeCloseTo(-0.2); // 浮点差值用近似比较
    expect(byName['mrr']?.delta).toBeCloseTo(-0.3);
    expect(byName['ndcg@5']).toMatchObject({ baseline: 0.9, candidate: null, delta: null });
    expect(byName['precision@3']).toMatchObject({ baseline: null, candidate: 0.5, delta: null });
    // 指标名排序稳定
    expect(rows.map((row) => row.metric)).toEqual(['mrr', 'ndcg@5', 'precision@3', 'recall@3']);
  });

  it('空输入返回空数组', () => {
    expect(diffRegressionMetrics({}, {})).toEqual([]);
  });
});

describe('质量回归面板', () => {
  it('空列表展示引导空态', () => {
    render(<QualityBoard runs={[]} listError={null} />);
    expect(screen.getByText('暂无回归运行')).toBeInTheDocument();
    expect(screen.getByText(/结果会出现在这里/)).toBeInTheDocument();
  });

  it('运行列表展示 state 徽标、指标 chips 与违规项', () => {
    render(<QualityBoard runs={[RUN_A, RUN_B]} listError={null} />);
    // 状态徽标：passed→DONE / failed→FAILED（既有 StateBadge 语义）
    expect(screen.getByText('DONE')).toBeInTheDocument();
    expect(screen.getByText('FAILED')).toBeInTheDocument();
    expect(screen.getByText('recall@3 1.0000')).toBeInTheDocument();
    expect(screen.getByTestId('violations')).toHaveTextContent('未达基线');
  });

  it('选择基线与候选后渲染指标差异表（delta 正数=提升）', () => {
    render(<QualityBoard runs={[RUN_A, RUN_B]} listError={null} />);
    expect(screen.queryByTestId('metric-diff')).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('基线运行'), { target: { value: 'run-a' } });
    fireEvent.change(screen.getByLabelText('候选运行'), { target: { value: 'run-b' } });
    const table = screen.getByTestId('metric-diff');
    expect(table).toHaveTextContent('recall@3');
    expect(table).toHaveTextContent('-0.2000');
    expect(table).toHaveTextContent('—'); // 单侧缺失指标
  });

  it('不足两次运行时隐藏对比区', () => {
    render(<QualityBoard runs={[RUN_A]} listError={null} />);
    expect(screen.queryByLabelText('基线运行')).not.toBeInTheDocument();
  });
});

describe('多黄金集过滤', () => {
  it('deriveGoldenSets 去重、排序并按名回退短 id', () => {
    const runs = [
      RUN_A,
      { ...RUN_B, golden_set_id: RUN_A.golden_set_id }, // 同集重复出现
      {
        ...RUN_B,
        id: 'run-c',
        golden_set_id: 'bbbbbbbb-1111-2222-3333-444444444444',
        golden_set_name: null, // 名缺失 → 回退短 id
      },
    ];
    expect(deriveGoldenSets(runs)).toEqual([
      { id: 'bbbbbbbb-1111-2222-3333-444444444444', label: 'bbbbbbbb…' },
      { id: 'aaaaaaaa-1111-2222-3333-444444444444', label: 'retrieval-smoke' },
    ]);
  });

  it('过滤下拉：选择集后回调传出 setId，展示集名', () => {
    const onSetChange = vi.fn();
    render(
      <QualityBoard
        runs={[RUN_A]}
        listError={null}
        goldenSets={deriveGoldenSets([RUN_A])}
        activeSetId={null}
        onSetChange={onSetChange}
      />,
    );
    fireEvent.change(screen.getByLabelText('按黄金集过滤'), {
      target: { value: RUN_A.golden_set_id },
    });
    expect(onSetChange).toHaveBeenCalledWith(RUN_A.golden_set_id);
    expect(screen.getByText('黄金集 retrieval-smoke')).toBeInTheDocument();
  });
});

describe('指标 diff 导出', () => {
  it('metricsDiffToCsv：单侧缺失留空、delta 仅双值输出', () => {
    const csv = metricsDiffToCsv(diffRegressionMetrics(RUN_A.aggregate, RUN_B.aggregate));
    const lines = csv.trim().split('\n');
    expect(lines[0]).toBe('metric,baseline,candidate,delta');
    const recall = lines.find((line) => line.startsWith('recall@3'));
    expect(recall).toBe('recall@3,1.000000,0.800000,-0.200000');
    const onlyA = lines.find((line) => line.startsWith('ndcg@5'));
    expect(onlyA).toBe('ndcg@5,0.900000,,'); // 候选缺失、delta 不计算
  });

  it('对比区渲染导出按钮且未选择时禁用', () => {
    render(<QualityBoard runs={[RUN_A, RUN_B]} listError={null} />);
    expect(screen.getByRole('button', { name: '导出 CSV' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('基线运行'), { target: { value: 'run-a' } });
    fireEvent.change(screen.getByLabelText('候选运行'), { target: { value: 'run-b' } });
    expect(screen.getByRole('button', { name: '导出 CSV' })).toBeEnabled();
  });
});

describe('逐案明细', () => {
  const PARSING_DETAIL: RegressionRunDetail = {
    ...RUN_A,
    cases: [
      {
        case: 0,
        reference_version_id: '11111111-aaaa-bbbb-cccc-000000000001',
        hypothesis_version_id: '22222222-aaaa-bbbb-cccc-000000000002',
        groups: { format: 'pdf', language: 'zh' },
        metrics: { text_cer: 0.0123, reading_order: 0.98 },
      },
      {
        reference_version_id: '33333333-aaaa-bbbb-cccc-000000000003',
        hypothesis_version_id: '44444444-aaaa-bbbb-cccc-000000000004',
        groups: {},
        metrics: {},
      },
    ],
  };

  it('默认不展示明细；点击后回调并提示尚未加载', () => {
    const onLoadDetail = vi.fn();
    render(<QualityBoard runs={[RUN_A]} listError={null} onLoadDetail={onLoadDetail} />);
    expect(screen.queryByTestId('regression-cases')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '查看明细' }));
    expect(onLoadDetail).toHaveBeenCalledWith('run-a');
    expect(screen.getByTestId('run-detail-run-a')).toHaveTextContent('尚未加载明细');
    expect(screen.getByRole('button', { name: '收起明细' })).toHaveAttribute('aria-expanded', 'true');
  });

  it('loading 态展示加载提示', () => {
    render(
      <QualityBoard
        runs={[RUN_A]}
        listError={null}
        detailLoadingId="run-a"
        onLoadDetail={() => undefined}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '查看明细' }));
    expect(screen.getByTestId('run-detail-run-a')).toHaveTextContent('明细加载中…');
  });

  it('回填后渲染版本对、分组与指标（不含正文）；再次点击收起', () => {
    render(
      <QualityBoard
        runs={[RUN_A]}
        listError={null}
        details={{ 'run-a': PARSING_DETAIL }}
        onLoadDetail={() => undefined}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '查看明细' }));
    const table = screen.getByTestId('regression-cases');
    expect(table).toHaveTextContent('11111111…');
    expect(table).toHaveTextContent('22222222…');
    expect(table).toHaveTextContent('format: pdf');
    expect(table).toHaveTextContent('text_cer 0.0123');
    expect(table).toHaveTextContent('reading_order 0.9800');
    // 无名组/无指标的第二行回退占位，不伪造 0
    expect(table).toHaveTextContent('—');
    expect(screen.getByRole('button', { name: '导出 cases CSV' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '收起明细' }));
    expect(screen.queryByTestId('regression-cases')).not.toBeInTheDocument();
  });

  it('检索回归以 query 展示用例', () => {
    const retrieval: RegressionRunDetail = {
      ...RUN_A,
      cases: [
        {
          query: '什么是检索增强生成？',
          groups: { language: 'zh' },
          metrics: { 'recall@3': 1 },
        },
      ],
    };
    render(
      <QualityBoard
        runs={[RUN_A]}
        listError={null}
        details={{ 'run-a': retrieval }}
        onLoadDetail={() => undefined}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '查看明细' }));
    expect(screen.getByTestId('regression-cases')).toHaveTextContent('什么是检索增强生成？');
  });

  it('空 cases 展示旧记录提示', () => {
    render(
      <QualityBoard
        runs={[RUN_A]}
        listError={null}
        details={{ 'run-a': { ...RUN_A, cases: [] } }}
        onLoadDetail={() => undefined}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '查看明细' }));
    expect(screen.getByTestId('run-detail-run-a')).toHaveTextContent('未记录逐案明细');
  });

  it('casesToCsv：逐案导出且逗号/引号正确转义', () => {
    const csv = casesToCsv([
      {
        case: 0,
        reference_version_id: 'r1',
        hypothesis_version_id: 'h1',
        groups: { format: 'pdf' },
        metrics: { text_cer: 0.0123 },
      },
      { query: '包含,逗号的问题', metrics: {} },
    ]);
    const lines = csv.trim().split('\n');
    expect(lines[0]).toBe(
      'case,query,reference_version_id,hypothesis_version_id,groups,metrics',
    );
    expect(lines[1]).toBe('1,,r1,h1,"{""format"":""pdf""}","{""text_cer"":0.0123}"');
    expect(lines[2]).toBe('2,"包含,逗号的问题",,,{},{}');
  });

  it('加载失败以 alert 展示', () => {
    render(
      <QualityBoard
        runs={[RUN_A]}
        listError={null}
        detailError={{ runId: 'run-a', message: '明细加载失败' }}
        onLoadDetail={() => undefined}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '查看明细' }));
    expect(screen.getByRole('alert')).toHaveTextContent('明细加载失败');
  });
});

describe('运行回归按钮', () => {
  const sets = deriveGoldenSets([RUN_A]);

  it('未选择黄金集时禁用；选择后点击回调传出 setId', () => {
    const onRun = vi.fn();
    const first = render(
      <QualityBoard
        runs={[RUN_A]}
        listError={null}
        goldenSets={sets}
        activeSetId={null}
        onSetChange={() => undefined}
        onRun={onRun}
      />,
    );
    expect(screen.getByRole('button', { name: '运行回归' })).toBeDisabled();
    first.unmount();

    render(
      <QualityBoard
        runs={[RUN_A]}
        listError={null}
        goldenSets={sets}
        activeSetId={RUN_A.golden_set_id}
        onSetChange={() => undefined}
        onRun={onRun}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '运行回归' }));
    expect(onRun).toHaveBeenCalledWith(RUN_A.golden_set_id);
  });

  it('running 态展示“运行中…”并禁用；完成后 runState 结果显式展示', () => {
    const { unmount } = render(
      <QualityBoard
        runs={[RUN_A]}
        listError={null}
        goldenSets={sets}
        activeSetId={RUN_A.golden_set_id}
        onSetChange={() => undefined}
        onRun={() => undefined}
        running
      />,
    );
    expect(screen.getByRole('button', { name: '运行中…' })).toBeDisabled();
    unmount();

    render(
      <QualityBoard
        runs={[RUN_A]}
        listError={null}
        goldenSets={sets}
        activeSetId={RUN_A.golden_set_id}
        onSetChange={() => undefined}
        onRun={() => undefined}
        runState={{ kind: 'ok', message: '运行完成：passed（run a1b2c3d4）' }}
      />,
    );
    expect(screen.getByTestId('run-state')).toHaveTextContent('运行完成：passed');
  });

  it('错误态以 alert 展示（如权限不足）', () => {
    render(
      <QualityBoard
        runs={[RUN_A]}
        listError={null}
        goldenSets={sets}
        activeSetId={RUN_A.golden_set_id}
        onSetChange={() => undefined}
        onRun={() => undefined}
        runState={{ kind: 'error', message: '需要租户管理员权限' }}
      />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent('需要租户管理员权限');
  });
});
