/** M4-6 adapter 注册表管理面板：平台管理员可见性、列表与注册/心跳。 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { AdapterInfo } from '../api/client';
import { AdapterRegistryPanel } from '../components/AdapterRegistryPanel';

const BUILTIN: AdapterInfo = {
  name: 'text-local',
  version: '0.1.0',
  mode: 'local',
  health: 'ready',
  source: 'builtin',
  stale: false,
};

const REGISTERED: AdapterInfo = {
  name: 'compliance-ocr',
  version: '1.2.0',
  mode: 'remote',
  health: 'degraded',
  reported_health: 'degraded',
  heartbeat_at: '2026-10-17T09:00:00Z',
  source: 'registered',
  stale: false,
};

function renderPanel(overrides: Partial<Parameters<typeof AdapterRegistryPanel>[0]> = {}) {
  return render(
    <AdapterRegistryPanel
      items={[BUILTIN, REGISTERED]}
      visible
      registering={false}
      heartbeatName={null}
      error={null}
      onRegister={() => undefined}
      onHeartbeat={() => undefined}
      {...overrides}
    />,
  );
}

describe('AdapterRegistryPanel', () => {
  it('非平台管理员不渲染', () => {
    renderPanel({ visible: false });
    expect(screen.queryByTestId('adapter-registry-panel')).not.toBeInTheDocument();
  });

  it('列表区分内置/外部；仅外部展示心跳按钮', () => {
    renderPanel();
    const table = screen.getByTestId('adapter-registry-table');
    const rows = within(table).getAllByRole('row');
    expect(rows).toHaveLength(3); // 表头 + 2 行
    const builtinRow = rows[1]!;
    const registeredRow = rows[2]!;
    expect(builtinRow).toHaveTextContent('text-local');
    expect(builtinRow).toHaveTextContent('内置');
    expect(builtinRow).not.toHaveTextContent('发送心跳');
    expect(registeredRow).toHaveTextContent('compliance-ocr');
    expect(registeredRow).toHaveTextContent('外部');
    expect(registeredRow).toHaveTextContent('degraded');
    expect(
      within(registeredRow).getByRole('button', { name: '发送心跳 compliance-ocr' }),
    ).toBeEnabled();
  });

  it('心跳点击传出名称；心跳中禁用并显示进度', () => {
    const onHeartbeat = vi.fn();
    renderPanel({ onHeartbeat });
    fireEvent.click(screen.getByRole('button', { name: '发送心跳 compliance-ocr' }));
    expect(onHeartbeat).toHaveBeenCalledWith('compliance-ocr');
  });

  it('心跳中禁用按钮并显示“心跳中…”', () => {
    renderPanel({ heartbeatName: 'compliance-ocr' });
    const button = screen.getByRole('button', { name: '发送心跳 compliance-ocr' });
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent('心跳中…');
  });

  it('注册提交前解析能力 JSON 并传出完整 payload', () => {
    const onRegister = vi.fn();
    renderPanel({ onRegister });
    fireEvent.change(screen.getByLabelText('adapter 名称'), { target: { value: 'layout-vlm' } });
    fireEvent.change(screen.getByLabelText('adapter 版本'), { target: { value: '0.3.0' } });
    fireEvent.change(screen.getByLabelText('adapter 模式'), { target: { value: 'composite' } });
    fireEvent.change(screen.getByLabelText('adapter 健康度'), { target: { value: 'maintenance' } });
    fireEvent.change(screen.getByLabelText('config 引用'), {
      target: { value: 'vault:adapters/layout-vlm' },
    });
    fireEvent.change(screen.getByLabelText('adapter 能力'), {
      target: { value: '{"formats":["pdf"],"ocr":true}' },
    });
    fireEvent.click(screen.getByRole('button', { name: '注册 / 更新' }));

    expect(onRegister).toHaveBeenCalledWith({
      name: 'layout-vlm',
      version: '0.3.0',
      mode: 'composite',
      health: 'maintenance',
      configRef: 'vault:adapters/layout-vlm',
      capabilities: { formats: ['pdf'], ocr: true },
    });
  });

  it('非法 JSON / 缺失名称在客户端拦截，不发请求', () => {
    const onRegister = vi.fn();
    renderPanel({ onRegister });

    fireEvent.change(screen.getByLabelText('adapter 名称'), { target: { value: 'x' } });
    fireEvent.change(screen.getByLabelText('adapter 能力'), { target: { value: '{oops' } });
    fireEvent.click(screen.getByRole('button', { name: '注册 / 更新' }));
    expect(screen.getByTestId('adapter-register-error')).toHaveTextContent('合法 JSON');
    expect(onRegister).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText('adapter 名称'), { target: { value: '  ' } });
    fireEvent.change(screen.getByLabelText('adapter 能力'), { target: { value: '{}' } });
    fireEvent.click(screen.getByRole('button', { name: '注册 / 更新' }));
    expect(screen.getByTestId('adapter-register-error')).toHaveTextContent('名称必填');
    expect(onRegister).not.toHaveBeenCalled();
  });

  it('提交中禁用按钮；服务端错误以 alert 展示', () => {
    renderPanel({ registering: true, error: 'adapter 名称需为 1-64 位小写字母/数字/._-' });
    expect(screen.getByRole('button', { name: '提交中…' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('小写字母');
  });

  it('空清单展示明确空态', () => {
    renderPanel({ items: [] });
    expect(screen.getByText('暂无 adapter 数据')).toBeInTheDocument();
  });
});
