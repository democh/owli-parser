/** 趋势图测试：坐标纯函数边界、组件渲染分支与指标序列派生。 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { RegressionRun } from '../api/client';
import { TrendChart, scaleSeries, type TrendPoint } from '../components/TrendChart';
import { deriveMetricSeries, listMetrics } from '../pages/QualityPage';

const POINTS: TrendPoint[] = [
  { runId: 'r1', label: '10/14 a1', value: 1.0, failed: false },
  { runId: 'r2', label: '10/15 b2', value: 0.8, failed: true },
  { runId: 'r3', label: '10/15 c3', value: 0.9, failed: false },
];

describe('scaleSeries（坐标纯函数）', () => {
  it('x 等距、y 按值域映射；min/max 如实返回', () => {
    const scaled = scaleSeries(POINTS, 100, 50, 10);
    expect(scaled).not.toBeNull();
    const series = scaled!;
    expect(series.min).toBe(0.8);
    expect(series.max).toBe(1.0);
    // 首点 x=padding，末点 x=width-padding，中间等距
    expect(series.points[0]?.x).toBe(10);
    expect(series.points[2]?.x).toBe(90);
    expect(series.points[1]?.x).toBe(50);
    // y 方向：最大值在上（y 小），最小值在下（y 大）
    expect(series.points[0]?.y).toBe(10);
    expect(series.points[1]?.y).toBe(40);
  });

  it('单点取中线；同值画水平线（span=0）', () => {
    const single = scaleSeries([POINTS[0]!], 100, 50, 10);
    expect(single?.points[0]?.x).toBe(50);
    expect(single?.points[0]?.y).toBe(25);

    const flat = scaleSeries(
      [POINTS[0]!, { ...POINTS[1]!, value: 1.0 }],
      100,
      50,
      10,
    );
    expect(flat?.points[0]?.y).toBe(flat?.points[1]?.y);
  });

  it('空序列返回 null（不伪造图）', () => {
    expect(scaleSeries([], 100, 50)).toBeNull();
  });
});

describe('TrendChart 渲染', () => {
  it('多点渲染折线与点；failed 点带警示语义', () => {
    render(<TrendChart points={POINTS} />);
    const chart = screen.getByTestId('trend-chart');
    expect(chart.querySelector('polyline')).not.toBeNull();
    expect(chart.querySelectorAll('circle')).toHaveLength(3);
    expect(chart.querySelector('circle.text-state-danger')).not.toBeNull();
    expect(chart).toHaveTextContent('min 0.8000');
    expect(chart).toHaveTextContent('max 1.0000');
  });

  it('单点不画折线但画点', () => {
    render(<TrendChart points={[POINTS[0]!]} />);
    const chart = screen.getByTestId('trend-chart');
    expect(chart.querySelector('polyline')).toBeNull();
    expect(chart.querySelectorAll('circle')).toHaveLength(1);
  });

  it('空序列显式提示', () => {
    render(<TrendChart points={[]} />);
    expect(screen.getByTestId('trend-empty')).toBeInTheDocument();
  });
});

function makeRun(overrides: Partial<RegressionRun> & { id: string }): RegressionRun {
  return {
    golden_set_id: 'set-1',
    golden_set_name: 'smoke',
    run_id: overrides.id,
    adapter_ref: 'lexical-bm25',
    config_digest: null,
    state: 'passed',
    created_at: '2026-10-15T10:00:00+08:00',
    aggregate: { 'recall@3': 1.0 },
    passed: true,
    violations: [],
    ...overrides,
  };
}

describe('趋势数据派生', () => {
  it('listMetrics：键并集排序（缺失不补 0）', () => {
    const runs = [
      makeRun({ id: 'a', aggregate: { 'recall@3': 1.0, mrr: 1.0 } }),
      makeRun({ id: 'b', aggregate: { mrr: 0.8, 'ndcg@5': 0.9 } }),
    ];
    expect(listMetrics(runs)).toEqual(['mrr', 'ndcg@5', 'recall@3']);
  });

  it('deriveMetricSeries：仅含该指标的 run、时间升序、failed 标记', () => {
    const runs = [
      makeRun({ id: 'late', created_at: '2026-10-15T11:00:00+08:00', aggregate: { mrr: 0.9 } }),
      makeRun({
        id: 'early',
        created_at: '2026-10-15T09:00:00+08:00',
        aggregate: { mrr: 1.0, 'recall@3': 1.0 },
      }),
      makeRun({
        id: 'noisy',
        state: 'failed',
        created_at: '2026-10-15T10:00:00+08:00',
        aggregate: { 'recall@3': 0.5 }, // 无 mrr → 排除在该指标序列外
      }),
    ];
    const series = deriveMetricSeries(runs, 'mrr');
    expect(series.map((point) => point.runId)).toEqual(['early', 'late']);
    expect(series.every((point) => !point.failed)).toBe(true);

    const recallSeries = deriveMetricSeries(runs, 'recall@3');
    expect(recallSeries.map((point) => point.runId)).toEqual(['early', 'noisy']);
    expect(recallSeries[1]?.failed).toBe(true);
    expect(recallSeries[1]?.value).toBe(0.5); // 如实取值，不补 0/1
  });
});
