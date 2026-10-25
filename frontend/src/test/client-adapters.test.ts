// @vitest-environment node
/**
 * adapter API 客户端请求级测试（MSW）：方法/路径/租户头/CSRF/请求体/错误映射。
 *
 * 组件测试验证 UI，这里验证真正发给后端的形状；契约路径另由 contract-drift 扫描。
 */
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import {
  adapterHeartbeat,
  adapters,
  registerAdapter,
  type AdapterInfo,
} from '../api/client';

const ROW: AdapterInfo = {
  name: 'compliance-ocr',
  version: '1.2.0',
  mode: 'remote',
  health: 'ready',
  reported_health: 'ready',
  heartbeat_at: '2026-10-17T09:00:00Z',
  stale: false,
  source: 'registered',
  config_ref: 'vault:adapters/compliance-ocr',
  capabilities: { formats: ['pdf'] },
};

const server = setupServer();
const BASE = 'http://owli.test';

beforeAll(() => {
  server.listen({ onUnhandledFrame: 'error' });
  // Node 环境无 origin：为客户端使用的相对路径补 base（MSW 已在 listen 时接管 fetch）
  const interceptedFetch = globalThis.fetch;
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' && input.startsWith('/') ? `${BASE}${input}` : input;
    return interceptedFetch(url, init);
  });
});
afterEach(() => server.resetHandlers());
afterAll(() => {
  server.close();
  vi.unstubAllGlobals();
});

describe('adapter API 客户端（MSW）', () => {
  it('GET 清单携带租户头且不发送 CSRF', async () => {
    let tenantHeader: string | null = null;
    let csrfHeader: string | null = null;
    server.use(
      http.get(`${BASE}/v1/adapters`, ({ request }) => {
        tenantHeader = request.headers.get('x-tenant-id');
        csrfHeader = request.headers.get('x-owli-csrf');
        return HttpResponse.json([ROW]);
      }),
    );

    await expect(adapters('tenant-1')).resolves.toEqual([ROW]);
    expect(tenantHeader).toBe('tenant-1');
    expect(csrfHeader).toBeNull();
  });

  it('PUT 注册：方法/路径/CSRF 与完整请求体', async () => {
    let method = '';
    let name = '';
    let csrfHeader: string | null = null;
    let body: unknown = null;
    server.use(
      http.put(`${BASE}/v1/adapters/:name`, async ({ request, params }) => {
        method = request.method;
        name = String(params.name);
        csrfHeader = request.headers.get('x-owli-csrf');
        body = await request.json();
        return HttpResponse.json({ ...ROW, name });
      }),
    );

    const result = await registerAdapter({
      name: 'compliance-ocr',
      version: '1.2.0',
      mode: 'remote',
      health: 'ready',
      configRef: 'vault:adapters/compliance-ocr',
      capabilities: { formats: ['pdf'] },
      tenantId: 'tenant-1',
      csrfToken: 'csrf-token',
    });

    expect(result.name).toBe('compliance-ocr');
    expect(method).toBe('PUT');
    expect(name).toBe('compliance-ocr');
    expect(csrfHeader).toBe('csrf-token');
    expect(body).toEqual({
      version: '1.2.0',
      mode: 'remote',
      capabilities: { formats: ['pdf'] },
      config_ref: 'vault:adapters/compliance-ocr',
      health: 'ready',
    });
  });

  it('心跳：POST 且未提供版本时省略 version 字段', async () => {
    let method = '';
    let body: unknown = null;
    server.use(
      http.post(`${BASE}/v1/adapters/:name/heartbeat`, async ({ request }) => {
        method = request.method;
        body = await request.json();
        return HttpResponse.json({ ...ROW, health: 'degraded' });
      }),
    );

    const result = await adapterHeartbeat({
      name: 'compliance-ocr',
      health: 'degraded',
      tenantId: 'tenant-1',
      csrfToken: 'csrf-token',
    });
    expect(result.health).toBe('degraded');
    expect(method).toBe('POST');
    expect(body).toEqual({ health: 'degraded' });
  });

  it('服务端错误映射为 ApiError（保留 code/message/trace）', async () => {
    server.use(
      http.put(`${BASE}/v1/adapters/:name`, () =>
        HttpResponse.json(
          {
            code: 'conflict',
            message: '内置 adapter 由进程内注册表管理，不允许注册同名外部实例',
            detail: {},
            trace_id: 'trace-1',
          },
          { status: 409 },
        ),
      ),
    );

    await expect(
      registerAdapter({
        name: 'text-local',
        version: '9.9.9',
        mode: 'remote',
        health: 'ready',
        capabilities: {},
        tenantId: 'tenant-1',
      }),
    ).rejects.toMatchObject({
      status: 409,
      code: 'conflict',
      message: '内置 adapter 由进程内注册表管理，不允许注册同名外部实例',
      traceId: 'trace-1',
    });
  });
});
