import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useJobEvents } from '../hooks/useJobEvents';
import { useSessionStore } from '../stores/session';

/** 最小 WebSocket 替身：记录实例并允许手动触发生命周期。 */
class MockWebSocket {
  static instances: MockWebSocket[] = [];
  url: string;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  close(): void {
    this.readyState = 3;
  }

  // 测试助手
  open(): void {
    this.readyState = 1;
    this.onopen?.();
  }

  emit(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }

  drop(): void {
    this.onclose?.();
  }
}

const JOB_SNAPSHOT = {
  id: 'job-1',
  document_id: '11111111-1111-1111-1111-111111111111',
  version_id: '22222222-2222-2222-2222-222222222222',
  dataset_id: '33333333-3333-3333-3333-333333333333',
  state: 'PARSING',
  stage: 'PARSING',
  attempt: 1,
  progress: {},
  total_units: 10,
  completed_units: 3,
  coverage: null,
  error: null,
  retrieval_ready: false,
  status_url: '/v1/parsing/jobs/job-1',
};

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

function makeFetchMock(behavior?: (url: string) => void) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    behavior?.(url);
    if (url.includes('/auth/ws-ticket')) {
      return jsonResponse({ ticket: 'ticket-1', expires_in_seconds: 60 });
    }
    if (url.includes('/parsing/jobs/') && url.includes('/events')) {
      return jsonResponse({ items: [], latest_seq: 0, cursor_expired: false });
    }
    if (url.includes('/parsing/jobs/')) {
      return jsonResponse({ ...JOB_SNAPSHOT, state: 'DONE', stage: 'DONE' });
    }
    void init;
    return new Response('not found', { status: 404 });
  });
}

beforeEach(() => {
  MockWebSocket.instances = [];
  vi.stubGlobal('WebSocket', MockWebSocket);
  useSessionStore.setState({
    user: { user_id: 'u1', username: 'u1', display_name: 'u1', is_platform_admin: false },
    csrfToken: 'csrf-token',
    tenants: [],
    currentTenantId: 'tenant-1',
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('useJobEvents（任务事件流）', () => {
  it('连接时携带租户与 CSRF 换取 ticket，URL 只含一次性 ticket', async () => {
    const fetchMock = makeFetchMock();
    vi.stubGlobal('fetch', fetchMock);
    renderHook(() => useJobEvents('job-1'));

    await waitFor(() => expect(MockWebSocket.instances.length).toBe(1));
    const ws = MockWebSocket.instances[0]!;
    expect(ws.url).toContain('/v1/ws/jobs/job-1?ticket=ticket-1');
    // ws-ticket 请求头带租户与 CSRF（长期凭据不进 URL）
    const ticketCall = fetchMock.mock.calls.find((call) =>
      String(call[0]).includes('/auth/ws-ticket'),
    )!;
    const headers = ticketCall[1]?.headers as Record<string, string>;
    expect(headers['X-Tenant-ID']).toBe('tenant-1');
    expect(headers['X-OWLI-CSRF']).toBe('csrf-token');
  });

  it('处理快照与增量事件，按 seq 去重合并', async () => {
    vi.stubGlobal('fetch', makeFetchMock());
    const { result } = renderHook(() => useJobEvents('job-1'));
    await waitFor(() => expect(MockWebSocket.instances.length).toBe(1));
    const ws = MockWebSocket.instances[0]!;

    act(() => ws.open());
    expect(result.current.mode).toBe('live');

    act(() =>
      ws.emit({ type: 'snapshot', job: JOB_SNAPSHOT, latest_seq: 2, truncated: false }),
    );
    act(() =>
      ws.emit({
        type: 'event',
        event: {
          seq: 3,
          type: 'job.state_changed',
          attempt: 1,
          from_state: 'PARSING',
          to_state: 'NORMALIZING',
          ts: '2026-10-15T12:00:00+00:00',
          detail: {},
        },
      }),
    );
    // 重复 seq 必须被去重
    act(() =>
      ws.emit({
        type: 'event',
        event: {
          seq: 3,
          type: 'job.state_changed',
          attempt: 1,
          from_state: 'PARSING',
          to_state: 'NORMALIZING',
          ts: '2026-10-15T12:00:00+00:00',
          detail: {},
        },
      }),
    );

    await waitFor(() => expect(result.current.job?.id).toBe('job-1'));
    expect(result.current.events).toHaveLength(1);
    expect(result.current.events[0]!.seq).toBe(3);
    expect(result.current.job?.completed_units).toBe(3);
  });

  it('终态快照后进入 closed 并停止重连', async () => {
    vi.stubGlobal('fetch', makeFetchMock());
    const { result } = renderHook(() => useJobEvents('job-1'));
    await waitFor(() => expect(MockWebSocket.instances.length).toBe(1));
    const ws = MockWebSocket.instances[0]!;
    act(() => ws.open());
    act(() =>
      ws.emit({
        type: 'snapshot',
        job: { ...JOB_SNAPSHOT, state: 'DONE', stage: 'DONE' },
        latest_seq: 9,
        truncated: false,
      }),
    );
    await waitFor(() => expect(result.current.mode).toBe('closed'));
    // 终态后连接关闭不再重建
    act(() => ws.drop());
    expect(MockWebSocket.instances.length).toBe(1);
  });

  it('WS 反复失败后降级为快照轮询', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let jobState: 'PARSING' | 'DONE' = 'PARSING';
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/auth/ws-ticket')) {
        throw new Error('ws-ticket unavailable');
      }
      if (url.includes('/parsing/jobs/') && url.includes('/events')) {
        return jsonResponse({ items: [], latest_seq: 0, cursor_expired: false });
      }
      if (url.includes('/parsing/jobs/')) {
        return jsonResponse({ ...JOB_SNAPSHOT, state: jobState, stage: jobState });
      }
      return new Response('not found', { status: 404 });
    });
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useJobEvents('job-1'));

    // 三次失败（含指数退避）后进入轮询（此时任务仍在运行中）
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    await waitFor(() => expect(result.current.mode).toBe('polling'));
    await waitFor(() => expect(result.current.job?.state).toBe('PARSING'));

    // 任务终态后轮询停止并进入 closed
    jobState = 'DONE';
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    await waitFor(() => expect(result.current.mode).toBe('closed'));
  });
});
