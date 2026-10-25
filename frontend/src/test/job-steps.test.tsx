/** 任务详情页解析步骤（L-4）：逐单位状态、错误码、截断提示与空/加载/错误态。 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { JobStepItem } from '../api/client';
import { JobStepsCard } from '../pages/TaskDetailPage';

const STEPS: JobStepItem[] = [
  {
    stage: 'PARSING',
    unit_id: 'page:0',
    attempt: 1,
    state: 'succeeded',
    checkpoint_ref: { engine: 'pypdf' },
    error_code: null,
    started_at: '2026-10-17T10:00:00Z',
    finished_at: '2026-10-17T10:00:01Z',
  },
  {
    stage: 'PARSING',
    unit_id: 'page:1',
    attempt: 1,
    state: 'failed',
    checkpoint_ref: { engine: 'pypdf' },
    error_code: 'parse_unit_failed',
    started_at: '2026-10-17T10:00:00Z',
    finished_at: '2026-10-17T10:00:02Z',
  },
];

function renderCard(overrides: Partial<Parameters<typeof JobStepsCard>[0]> = {}) {
  return render(
    <JobStepsCard steps={STEPS} total={2} limit={1000} pending={false} error={false} {...overrides} />,
  );
}

describe('JobStepsCard（解析步骤）', () => {
  it('逐单位展示阶段/attempt/状态/错误码与总数', () => {
    renderCard();
    expect(screen.getByTestId('job-steps-table')).toBeInTheDocument();
    expect(screen.getAllByRole('row')).toHaveLength(3); // 表头 + 2 行
    expect(screen.getByText('解析步骤（页级单位） · 2')).toBeInTheDocument();
    expect(screen.getByText('page:0')).toBeInTheDocument();
    expect(screen.getByText('page:1')).toBeInTheDocument();
    expect(screen.getByText('成功')).toBeInTheDocument();
    expect(screen.getByText('失败')).toBeInTheDocument();
    expect(screen.getByText('parse_unit_failed')).toBeInTheDocument();
  });

  it('无步骤时提示解析完成后登记', () => {
    renderCard({ steps: [], total: 0 });
    expect(screen.getByText('暂无步骤：解析完成后按页/文档登记。')).toBeInTheDocument();
    expect(screen.queryByTestId('job-steps-table')).not.toBeInTheDocument();
  });

  it('加载中与加载失败状态如实展示', () => {
    const { unmount } = renderCard({ steps: [], total: 0, pending: true });
    expect(screen.getByText('加载中…')).toBeInTheDocument();
    unmount();

    renderCard({ steps: [], total: 0, error: true });
    expect(screen.getByRole('alert')).toHaveTextContent('步骤加载失败');
  });

  it('超出 limit 时明确展示截断比例', () => {
    renderCard({ total: 5, limit: 2 });
    expect(screen.getByText('已显示 2/5 条（limit=2），可用 stage 过滤或提高 limit。')).toBeInTheDocument();
  });
});
