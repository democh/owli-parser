import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { StateBadge, EmptyState } from '../components/shared';

describe('shared 组件', () => {
  it('状态徽章渲染文本标签（不仅靠颜色）', () => {
    render(<StateBadge state="DONE" />);
    expect(screen.getByText('DONE')).toBeInTheDocument();
  });

  it('空态展示标题', () => {
    render(<EmptyState title="暂无数据" />);
    expect(screen.getByText('暂无数据')).toBeInTheDocument();
  });
});
