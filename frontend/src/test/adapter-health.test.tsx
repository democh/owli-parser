/** M4-6 adapter 健康条：聚合口径与展示降噪。 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { AdapterInfo } from '../api/client';
import { AdapterHealthStrip, summarizeAdapterHealth } from '../components/AdapterHealthStrip';

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
  source: 'registered',
  stale: false,
};

const STALE: AdapterInfo = {
  ...REGISTERED,
  name: 'legacy-mathtype',
  health: 'unavailable',
  reported_health: 'ready',
  stale: true,
};

describe('summarizeAdapterHealth', () => {
  it('全部 ready 且无外部实例时为 ok', () => {
    expect(summarizeAdapterHealth([])).toMatchObject({ status: 'ok', total: 0, registered: 0 });
    const summary = summarizeAdapterHealth([BUILTIN]);
    expect(summary.status).toBe('ok');
    expect(summary.counts.ready).toBe(1);
    expect(summary.registered).toBe(0);
  });

  it('degraded/maintenance/unknown → degraded；unavailable 优先', () => {
    expect(summarizeAdapterHealth([BUILTIN, REGISTERED]).status).toBe('degraded');
    expect(
      summarizeAdapterHealth([BUILTIN, { ...REGISTERED, health: 'maintenance' }]).status,
    ).toBe('degraded');
    expect(summarizeAdapterHealth([BUILTIN, { ...REGISTERED, health: 'unknown' }]).status).toBe(
      'degraded',
    );
    expect(summarizeAdapterHealth([BUILTIN, REGISTERED, STALE]).status).toBe('unavailable');
  });

  it('失联清单按 stale 标记收集，外部数量单独计数', () => {
    const summary = summarizeAdapterHealth([BUILTIN, REGISTERED, STALE]);
    expect(summary.total).toBe(3);
    expect(summary.registered).toBe(2);
    expect(summary.stale).toEqual(['legacy-mathtype']);
    expect(summary.counts).toMatchObject({ ready: 1, degraded: 1, unavailable: 1 });
  });
});

describe('AdapterHealthStrip', () => {
  it('默认安装（仅内置且 ok）不展示，避免噪音', () => {
    render(<AdapterHealthStrip items={[BUILTIN]} />);
    expect(screen.queryByTestId('adapter-health')).not.toBeInTheDocument();
  });

  it('存在外部实例时展示状态、数量与失联名单', () => {
    render(<AdapterHealthStrip items={[BUILTIN, REGISTERED]} />);
    const strip = screen.getByTestId('adapter-health');
    expect(strip).toHaveTextContent('adapter degraded');
    expect(strip).toHaveTextContent('共 2（外部 1）');
    expect(strip).toHaveTextContent('degraded 1');
    expect(strip).not.toHaveTextContent('失联');
  });

  it('失联时以警示文案列出 adapter 名', () => {
    render(<AdapterHealthStrip items={[BUILTIN, STALE]} />);
    const strip = screen.getByTestId('adapter-health');
    expect(strip).toHaveTextContent('adapter unavailable');
    expect(strip).toHaveTextContent('失联：legacy-mathtype');
  });
});
