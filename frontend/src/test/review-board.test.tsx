/**
 * 复核队列（纯组件）测试：空态、列表渲染、处置交互（动作白名单 + 备注）、
 * CAS 冲突提示、状态切换回调。
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import type { ReviewItem } from '../api/client';
import { ReviewBoard } from '../pages/ReviewPage';

const OPEN_ITEM: ReviewItem = {
  id: 'r-1',
  version_id: '11111111-2222-3333-4444-555555555555',
  document_id: 'dddddddd-2222-3333-4444-555555555555',
  job_id: '99999999-2222-3333-4444-555555555555',
  block_id: 'blk-7',
  reason: 'low_confidence',
  status: 'open',
  reviewer_id: null,
  resolution: null,
  created_at: '2026-10-15T10:00:00+08:00',
};

const RESOLVED_ITEM: ReviewItem = {
  ...OPEN_ITEM,
  id: 'r-2',
  status: 'resolved',
  reviewer_id: 'aaaaaaaa-2222-3333-4444-555555555555',
  resolution: {
    action: 'rerun_page',
    note: '该页扫描倾斜，重跑后确认',
    resolved_at: '2026-10-15T11:00:00+08:00',
  },
};

function renderBoard(overrides: Partial<Parameters<typeof ReviewBoard>[0]> = {}) {
  return render(
    <MemoryRouter>
      <ReviewBoard
        items={[OPEN_ITEM]}
        status="open"
        onStatusChange={() => undefined}
        onResolve={() => undefined}
        pending={false}
        listError={null}
        resolveState={null}
        {...overrides}
      />
    </MemoryRouter>,
  );
}

describe('复核队列', () => {
  it('空态按状态区分文案', () => {
    renderBoard({ items: [] });
    expect(screen.getByText('没有待处置的复核项')).toBeInTheDocument();

    renderBoard({ items: [], status: 'resolved' });
    expect(screen.getByText('暂无已处置记录')).toBeInTheDocument();
  });

  it('列表渲染原因、block 与任务链接', () => {
    renderBoard();
    expect(screen.getByText('low_confidence')).toBeInTheDocument();
    expect(screen.getByText('block blk-7')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /任务 99999999/ })).toHaveAttribute(
      'href',
      '/tasks/99999999-2222-3333-4444-555555555555',
    );
  });

  it('document_id 存在时提供工作台定位链接（携带版本与块），缺失时不渲染', () => {
    const first = renderBoard();
    expect(screen.getByRole('link', { name: /打开工作台定位/ })).toHaveAttribute(
      'href',
      '/documents/dddddddd-2222-3333-4444-555555555555?version_id=11111111-2222-3333-4444-555555555555&block_id=blk-7',
    );
    first.unmount();

    renderBoard({ items: [{ ...OPEN_ITEM, id: 'r-x', document_id: null }] });
    expect(screen.queryByRole('link', { name: /打开工作台定位/ })).not.toBeInTheDocument();
  });

  it('已处置项展示 resolution（动作/备注），不显示处置按钮', () => {
    renderBoard({ items: [RESOLVED_ITEM], status: 'resolved' });
    expect(screen.getByText(/rerun_page · 该页扫描倾斜，重跑后确认/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '处置' })).not.toBeInTheDocument();
  });

  it('处置交互：动作白名单与备注随回调提交', () => {
    const onResolve = vi.fn();
    renderBoard({ onResolve });
    fireEvent.click(screen.getByRole('button', { name: '处置' }));

    const select = screen.getByLabelText('处置动作');
    expect(
      [...select.querySelectorAll('option')].map((option) => option.textContent),
    ).toEqual(['接受当前结果', '人工修正', '重跑该页', '切换适配器', '忽略（记录理由）']);
    fireEvent.change(select, { target: { value: 'rerun_page' } });
    fireEvent.change(screen.getByLabelText('处置备注'), {
      target: { value: '扫描倾斜' },
    });
    fireEvent.click(screen.getByRole('button', { name: '确认处置' }));

    expect(onResolve).toHaveBeenCalledWith('r-1', 'rerun_page', '扫描倾斜');
  });

  it('CAS 冲突提示以 alert 展示', () => {
    renderBoard({
      resolveState: { itemId: 'r-1', kind: 'error', message: '该复核项已被处置（409），已刷新队列。' },
    });
    expect(screen.getByTestId('resolve-state')).toHaveTextContent('409');
    expect(screen.getByRole('alert')).toBeInTheDocument();
  });

  it('状态切换回调传出目标状态', () => {
    const onStatusChange = vi.fn();
    renderBoard({ onStatusChange });
    fireEvent.click(screen.getByRole('button', { name: '已处置' }));
    expect(onStatusChange).toHaveBeenCalledWith('resolved');
  });
});

describe('复核页过滤条', () => {
  it('无过滤时不渲染过滤条', () => {
    renderBoard();
    expect(screen.queryByTestId('review-filter')).not.toBeInTheDocument();
  });

  it('version_id 过滤展示与清除回调', () => {
    const onFilterChange = vi.fn();
    renderBoard({
      filter: { versionId: '11111111-2222-3333-4444-555555555555', reason: null },
      onFilterChange,
    });
    const bar = screen.getByTestId('review-filter');
    expect(bar).toHaveTextContent('版本 11111111…');

    fireEvent.click(screen.getByRole('button', { name: '清除过滤' }));
    expect(onFilterChange).toHaveBeenCalledWith({ versionId: null, reason: null });
  });

  it('原因下拉切换保留版本过滤', () => {
    const onFilterChange = vi.fn();
    renderBoard({
      filter: { versionId: 'v-1', reason: null },
      onFilterChange,
    });
    fireEvent.change(screen.getByLabelText('按原因过滤'), {
      target: { value: 'low_confidence' },
    });
    expect(onFilterChange).toHaveBeenCalledWith({
      versionId: 'v-1',
      reason: 'low_confidence',
    });
  });
});
