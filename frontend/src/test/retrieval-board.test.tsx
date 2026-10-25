/**
 * 召回测试台（纯组件）测试：
 * - 未启用时如实展示 capability_not_ready，不显示假结果；
 * - 结果表展示每通道 rank/原始分/RRF 贡献与低相关警告；
 * - payload.document_id 存在时提供工作台跳转，否则显示占位。
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { ApiError, type RetrievalTestResponse } from '../api/client';
import {
  RetrievalBoard,
  buildTunables,
  diffRetrievals,
  diffToCsv,
  diffToJson,
} from '../pages/RetrievalPage';

const DATASETS = [{ id: 'ds-1', name: '产品知识库' }];

function capabilityError(): ApiError {
  return new ApiError(
    503,
    {
      code: 'capability_not_ready',
      message: '检索通道在 M3 里程碑启用（retrieval.enabled=false）',
      detail: {},
      trace_id: 'trace-1',
    },
    null,
  );
}

const RESULT: RetrievalTestResponse = {
  items: [
    {
      id: 't1:v1:c1:1:1',
      rrf_score: 0.0325,
      channel_ranks: { dense: 1, lexical: 2 },
      channel_raw_scores: { dense: 0.87, lexical: 12.5 },
      channel_contributions: { dense: 0.0164, lexical: 0.0161 },
      rerank_score: null,
      payload: { chunk_id: 'aaaaaaaa-1111-2222-3333-444444444444', document_id: 'doc-9' },
    },
    {
      id: 't1:v1:c2:1:1',
      rrf_score: 0.0161,
      channel_ranks: { lexical: 1 },
      channel_raw_scores: { lexical: 14.0 },
      channel_contributions: { lexical: 0.0161 },
      rerank_score: null,
      payload: { chunk_id: 'bbbbbbbb-1111-2222-3333-444444444444' },
    },
  ],
  channels_used: ['dense', 'lexical'],
  applied_filters: { tenant_id: 'tenant-1', dataset_ids: ['ds-1'], acl_refs: ['dataset:ds-1'] },
  low_relevance: false,
  warnings: [],
  applied_params: {
    top_k: 10,
    per_channel_limit: 50,
    candidate_limit: 100,
    rrf_k: 60,
    channel_weights: { dense: 1, lexical: 1 },
    min_relevance_threshold: 0,
    rerank_applied: true,
  },
};

const [resultItem0, resultItem1] = RESULT.items;
if (!resultItem0 || !resultItem1) {
  throw new Error('测试 fixture 需要至少两个结果项');
}

const RESULT_B: RetrievalTestResponse = {
  ...RESULT,
  items: [
    resultItem1, // c2 从 #2 → #1
    resultItem0, // c1 从 #1 → #2
    {
      id: 't1:v1:c9:1:1',
      rrf_score: 0.01,
      channel_ranks: { lexical: 3 },
      channel_raw_scores: { lexical: 3.5 },
      channel_contributions: { lexical: 0.01 },
      rerank_score: null,
      payload: { chunk_id: 'cccccccc-1111-2222-3333-444444444444' },
    },
  ],
};

function renderBoard(overrides: Partial<Parameters<typeof RetrievalBoard>[0]> = {}) {
  return render(
    <MemoryRouter>
      <RetrievalBoard
        datasets={DATASETS}
        pending={false}
        error={null}
        result={null}
        onSubmit={() => undefined}
        {...overrides}
      />
    </MemoryRouter>,
  );
}

describe('buildTunables（高级参数收集）', () => {
  it('留空字段不进入 tunables（取服务端配置）', () => {
    expect(buildTunables({ rrfK: '', perChannel: '', denseWeight: '', lexicalWeight: '', threshold: '' })).toEqual({});
  });

  it('非法输入（负数/非数字/零）被忽略；阈值允许 0', () => {
    expect(
      buildTunables({
        rrfK: '-1',
        perChannel: 'abc',
        denseWeight: '0',
        lexicalWeight: '',
        threshold: '0',
      }),
    ).toEqual({ channel_weights: { dense: 0 }, min_relevance_threshold: 0 });
  });

  it('完整输入映射为后端字段名', () => {
    expect(
      buildTunables({
        rrfK: '15',
        perChannel: '30',
        denseWeight: '1',
        lexicalWeight: '3',
        threshold: '2.5',
      }),
    ).toEqual({
      rrf_k: 15,
      per_channel_limit: 30,
      channel_weights: { dense: 1, lexical: 3 },
      min_relevance_threshold: 2.5,
    });
  });
});

describe('召回测试台', () => {
  it('检索通道未启用时展示 capability_not_ready 说明，不显示结果表', () => {
    renderBoard({ error: capabilityError() });
    expect(screen.getByTestId('retrieval-not-ready')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('结果表展示通道 rank/原始分/RRF 贡献与过滤条件', () => {
    renderBoard({ result: RESULT });
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getAllByText('dense').length).toBeGreaterThan(0);
    expect(screen.getAllByText('lexical').length).toBeGreaterThan(0);
    expect(screen.getByText(/dense #1 · 0\.8700 · \+0\.0164/)).toBeInTheDocument();
    expect(screen.getByText(/lexical #2 · 12\.5000 · \+0\.0161/)).toBeInTheDocument();
    expect(screen.getByText('0.0325')).toBeInTheDocument();
    expect(screen.getByText(/数据集 ds-1/)).toBeInTheDocument();
  });

  it('低相关与空召回警告显式展示', () => {
    renderBoard({
      result: {
        ...RESULT,
        items: [],
        low_relevance: true,
        warnings: ['所有召回低于相关性阈值，结果可能不相关'],
      },
    });
    expect(screen.getByTestId('retrieval-warnings')).toBeInTheDocument();
    expect(screen.getByText(/low_relevance=true/)).toBeInTheDocument();
    expect(screen.getByText('无召回结果')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('payload.document_id 存在时提供工作台跳转，缺失时显示占位', () => {
    renderBoard({ result: RESULT });
    const links = screen.getAllByRole('link', { name: '打开工作台' });
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute('href', '/documents/doc-9');
    expect(screen.getByText('—')).toBeInTheDocument();
  });

  it('未选择数据集时禁用提交；提交回调携带查询与范围', () => {
    const onSubmit = vi.fn();
    renderBoard({ onSubmit });
    const submit = screen.getByRole('button', { name: '执行检索' });
    expect(submit).toBeDisabled();

    fireEvent.change(screen.getByLabelText('查询词'), { target: { value: '检索增强' } });
    fireEvent.click(screen.getByRole('checkbox', { name: '产品知识库' }));
    expect(submit).not.toBeDisabled();
    fireEvent.click(submit);

    expect(onSubmit).toHaveBeenCalledWith({
      query: '检索增强',
      dataset_ids: ['ds-1'],
      top_k: 10,
      tunables: {},
    });
  });

  it('高级参数面板：填写 rrf_k 与权重后随提交传入 tunables', () => {
    const onSubmit = vi.fn();
    renderBoard({ onSubmit });
    expect(screen.queryByLabelText('RRF k')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '高级参数' }));
    fireEvent.change(screen.getByLabelText('查询词'), { target: { value: '参数调优' } });
    fireEvent.click(screen.getByRole('checkbox', { name: '产品知识库' }));
    fireEvent.change(screen.getByLabelText('RRF k'), { target: { value: '10' } });
    fireEvent.change(screen.getByLabelText('lexical 权重'), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText('低相关阈值'), { target: { value: '0' } });
    fireEvent.click(screen.getByRole('button', { name: '执行检索' }));

    expect(onSubmit).toHaveBeenCalledWith({
      query: '参数调优',
      dataset_ids: ['ds-1'],
      top_k: 10,
      tunables: { rrf_k: 10, channel_weights: { lexical: 2 }, min_relevance_threshold: 0 },
    });
  });

  it('结果区回显实际生效参数（applied_params）', () => {
    renderBoard({ result: RESULT });
    const params = screen.getByTestId('applied-params');
    expect(params).toHaveTextContent('RRF k=60');
    expect(params).toHaveTextContent('权重 lexical=1');
    expect(params).toHaveTextContent('候选上限=100');
    expect(screen.getByTestId('rerank-applied')).toHaveTextContent('rerank 已生效');
  });

  it('rerank 未生效（未配置绑定）时不显示徽标', () => {
    renderBoard({
      result: { ...RESULT, applied_params: { ...RESULT.applied_params, rerank_applied: false } },
    });
    expect(screen.queryByTestId('rerank-applied')).not.toBeInTheDocument();
  });
});

describe('A/B 参数对比', () => {
  it('diffRetrievals：仅 A/仅 B/共有与平均排名变化（正数=B 前移）', () => {
    const diff = diffRetrievals(RESULT, RESULT_B);
    expect(diff.onlyA).toBe(0);
    expect(diff.onlyB).toBe(1);
    expect(diff.common).toBe(2);
    // c1: rankA 1 → rankB 2（-1）；c2: rankA 2 → rankB 1（+1）→ 平均 0
    expect(diff.meanRankShift).toBeCloseTo(0);
    const c1 = diff.rows.find((row) => row.chunkId.startsWith('aaaaaaaa'));
    expect(c1?.rankA).toBe(1);
    expect(c1?.rankB).toBe(2);
    const onlyB = diff.rows.find((row) => row.chunkId.startsWith('cccccccc'));
    expect(onlyB?.rankA).toBeNull();
    expect(onlyB?.rankB).toBe(3);
  });

  it('diffRetrievals：单项未命中侧标记 null；空结果安全', () => {
    const empty = diffRetrievals(RESULT, { ...RESULT, items: [] });
    expect(empty.onlyA).toBe(2);
    expect(empty.onlyB).toBe(0);
    expect(empty.meanRankShift).toBeNull();
  });

  it('对比模式：B 组参数面板出现，提交回调携带 tunablesB', () => {
    const onSubmit = vi.fn();
    renderBoard({ onSubmit });
    expect(screen.queryByLabelText('B 组 RRF k')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('checkbox', { name: 'A/B 对比模式' }));
    fireEvent.change(screen.getByLabelText('查询词'), { target: { value: '对比实验' } });
    fireEvent.click(screen.getByRole('checkbox', { name: '产品知识库' }));
    fireEvent.change(screen.getByLabelText('B 组 RRF k'), { target: { value: '10' } });
    fireEvent.change(screen.getByLabelText('B 组 lexical 权重'), { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: '执行对比' }));

    expect(onSubmit).toHaveBeenCalledWith({
      query: '对比实验',
      dataset_ids: ['ds-1'],
      top_k: 10,
      tunables: {},
      tunablesB: { rrf_k: 10, channel_weights: { lexical: 3 } },
    });
  });

  it('resultB 存在时渲染 diff 视图（摘要 + 逐项并排 + 仅 B 标记）', () => {
    renderBoard({ result: RESULT, resultB: RESULT_B });
    expect(screen.getByTestId('diff-summary')).toHaveTextContent('仅 B 命中 1 项');
    expect(screen.getByTestId('diff-summary')).toHaveTextContent('共有 2 项');
    expect(screen.getByText('仅 B')).toBeInTheDocument();
    // 匹配项展示两侧排名与 RRF
    expect(screen.getByText(/#2 · 0\.0325/)).toBeInTheDocument();
  });
});

describe('对比结果导出', () => {
  it('diffToCsv：表头 + 行；仅单侧命中的排名列留空', () => {
    const csv = diffToCsv(diffRetrievals(RESULT, RESULT_B));
    const lines = csv.trim().split('\n');
    expect(lines[0]).toBe('chunk_id,rank_a,rank_b,rrf_a,rrf_b,delta_rank');
    const c1 = lines.find((line) => line.startsWith('aaaaaaaa'));
    expect(c1?.split(',').slice(0, 3)).toEqual([
      'aaaaaaaa-1111-2222-3333-444444444444',
      '1',
      '2',
    ]);
    const onlyB = lines.find((line) => line.startsWith('cccccccc'));
    expect(onlyB).toContain(',3,'); // rank_a 留空、rank_b=3
    expect(onlyB?.split(',')[1]).toBe('');
  });

  it('diffToJson：包含两组生效参数、摘要与逐项行', () => {
    const parsed = JSON.parse(diffToJson(RESULT, RESULT_B, diffRetrievals(RESULT, RESULT_B)));
    expect(parsed.payload.a.applied_params.rrf_k).toBe(60);
    expect(parsed.payload.b.applied_params.candidate_limit).toBe(100);
    expect(parsed.summary).toMatchObject({ only_a: 0, only_b: 1, common: 2 });
    expect(parsed.rows).toHaveLength(3);
  });

  it('diff 视图渲染导出按钮', () => {
    renderBoard({ result: RESULT, resultB: RESULT_B });
    expect(screen.getByRole('button', { name: '导出 CSV' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '导出 JSON' })).toBeInTheDocument();
  });
});
