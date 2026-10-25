import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { IssuedToken } from '../api/client';
import { TokenSecretDialog } from '../components/TokenSecretDialog';

const ISSUED: IssuedToken = {
  token_id: '11111111-1111-1111-1111-111111111111',
  public_id: 'abcdef012345',
  token: 'owli_pat_abcdef012345_secret-value-0123456789abcdef',
  expires_at_epoch: 1900000000,
  scopes: ['datasets:read'],
  dataset_ids: ['22222222-2222-2222-2222-222222222222'],
  secret_available: true,
};

describe('TokenSecretDialog（一次性密钥弹窗）', () => {
  it('默认打码展示，显式点击后才显示明文', () => {
    render(<TokenSecretDialog issued={ISSUED} onClose={() => undefined} />);
    const code = screen.getByTestId('token-secret-value');
    // 默认不展示明文（防止截图/录屏直接泄露）
    expect(code.textContent).not.toContain(ISSUED.token);
    fireEvent.click(screen.getByRole('button', { name: '显示' }));
    expect(code.textContent).toBe(ISSUED.token);
  });

  it('提示关闭后无法再次查看，并提供显式关闭', () => {
    const onClose = vi.fn();
    render(<TokenSecretDialog issued={ISSUED} onClose={onClose} />);
    expect(screen.getByRole('alert').textContent).toContain('关闭本窗口后无法再次查看');
    fireEvent.click(screen.getByRole('button', { name: '我已保存，关闭' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('复制使用剪贴板且不回传掩码', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<TokenSecretDialog issued={ISSUED} onClose={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: '复制' }));
    expect(writeText).toHaveBeenCalledWith(ISSUED.token);
    expect(await screen.findByText('已复制')).toBeInTheDocument();
  });

  it('弹窗具备对话框语义与标题（无障碍）', () => {
    render(<TokenSecretDialog issued={ISSUED} onClose={() => undefined} />);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(screen.getByRole('heading', { name: 'Token 已签发' })).toBeInTheDocument();
  });
});
