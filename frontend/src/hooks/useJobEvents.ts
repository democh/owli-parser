/**
 * useJobEvents：任务事件流（事件优先、快照轮询降级）。
 *
 * - 连接前经 POST /auth/ws-ticket 获取租户绑定的一次性 ticket（长期凭据不进 URL）。
 * - 服务端先发 snapshot 与 latest_seq，再按 seq 增量推送；本 hook 按 seq 去重合并。
 * - 断线指数退避重连；连续失败达阈值后切换为快照轮询（降级），终态后停止。
 * - 组件卸载 / 切换任务：关闭连接并清理计时器（不泄漏旧任务事件）。
 */
import { useEffect, useRef, useState } from 'react';
import { request, type JobItem } from '../api/client';
import { useSessionStore } from '../stores/session';

export interface JobEventItem {
  seq: number;
  type: string;
  attempt: number | null;
  from_state: string | null;
  to_state: string | null;
  ts: string;
  detail: Record<string, unknown>;
}

export type JobStreamMode = 'connecting' | 'live' | 'polling' | 'closed';

const TERMINAL_STATES = ['DONE', 'PARTIAL_DONE', 'FAILED', 'DEAD_LETTER', 'CANCELLED'];
const MAX_WS_ATTEMPTS = 3;
const POLL_ACTIVE_MS = 4000;
const POLL_ERROR_MS = 8000;

interface WsSnapshotMessage {
  type: 'snapshot';
  job: JobItem;
  latest_seq: number;
}

interface WsEventMessage {
  type: 'event';
  event: JobEventItem;
}

interface WsControlMessage {
  type: 'heartbeat' | 'closed';
  state?: string;
}

type WsIncoming = WsSnapshotMessage | WsEventMessage | WsControlMessage;

export function useJobEvents(jobId: string | null): {
  mode: JobStreamMode;
  job: JobItem | null;
  events: JobEventItem[];
} {
  const tenantId = useSessionStore((s) => s.currentTenantId);
  const csrfToken = useSessionStore((s) => s.csrfToken);
  const [mode, setMode] = useState<JobStreamMode>('connecting');
  const [job, setJob] = useState<JobItem | null>(null);
  const [events, setEvents] = useState<JobEventItem[]>([]);
  const lastSeqRef = useRef(0);

  useEffect(() => {
    if (!jobId || !tenantId) {
      return;
    }
    // 切换任务时重置累积状态
    lastSeqRef.current = 0;
    setEvents([]);
    setJob(null);

    let disposed = false;
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let attempts = 0;
    let closedByServer = false;

    function mergeEvents(incoming: JobEventItem[]): void {
      if (incoming.length === 0) return;
      setEvents((prev) => {
        const bySeq = new Map<number, JobEventItem>(prev.map((e) => [e.seq, e]));
        for (const event of incoming) {
          bySeq.set(event.seq, event);
        }
        return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
      });
      lastSeqRef.current = Math.max(lastSeqRef.current, incoming[incoming.length - 1]!.seq);
    }

    function startPolling(): void {
      setMode('polling');
      const poll = async (): Promise<void> => {
        if (disposed) return;
        try {
          const snapshot = await request<JobItem>(`/parsing/jobs/${jobId}`, { tenantId });
          if (disposed) return;
          setJob(snapshot);
          const delta = await request<{ items: JobEventItem[]; latest_seq: number }>(
            `/parsing/jobs/${jobId}/events?after_seq=${lastSeqRef.current}`,
            { tenantId },
          );
          if (disposed) return;
          mergeEvents(delta.items);
          if (TERMINAL_STATES.includes(snapshot.state)) {
            setMode('closed');
            return;
          }
          timer = setTimeout(() => void poll(), POLL_ACTIVE_MS);
        } catch {
          timer = setTimeout(() => void poll(), POLL_ERROR_MS);
        }
      };
      void poll();
    }

    async function connect(): Promise<void> {
      if (disposed) return;
      setMode('connecting');
      try {
        const ticket = await request<{ ticket: string }>('/auth/ws-ticket', {
          method: 'POST',
          tenantId,
          csrfToken,
          body: { tenant_id: tenantId },
        });
        if (disposed) return;
        const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
        ws = new WebSocket(
          `${scheme}://${window.location.host}/v1/ws/jobs/${jobId}?ticket=${encodeURIComponent(ticket.ticket)}`,
        );
        ws.onopen = () => {
          attempts = 0;
          setMode('live');
        };
        ws.onmessage = (message: MessageEvent<string>) => {
          let payload: WsIncoming;
          try {
            payload = JSON.parse(message.data) as WsIncoming;
          } catch {
            return;
          }
          if (payload.type === 'snapshot') {
            setJob(payload.job);
            lastSeqRef.current = Math.max(lastSeqRef.current, payload.latest_seq);
            if (TERMINAL_STATES.includes(payload.job.state)) {
              closedByServer = true;
              setMode('closed');
              ws?.close(1000);
            }
            return;
          }
          if (payload.type === 'event') {
            mergeEvents([payload.event]);
            return;
          }
          if (payload.type === 'closed') {
            closedByServer = true;
            setMode('closed');
            ws?.close(1000);
          }
          // heartbeat：忽略
        };
        ws.onclose = () => {
          if (disposed || closedByServer) return;
          attempts += 1;
          if (attempts >= MAX_WS_ATTEMPTS) {
            startPolling();
            return;
          }
          timer = setTimeout(() => void connect(), Math.min(1000 * 2 ** attempts, 15000));
        };
        ws.onerror = () => {
          ws?.close();
        };
      } catch {
        attempts += 1;
        if (attempts >= MAX_WS_ATTEMPTS) {
          startPolling();
          return;
        }
        timer = setTimeout(() => void connect(), Math.min(1000 * 2 ** attempts, 15000));
      }
    }

    void connect();
    return () => {
      disposed = true;
      if (timer !== null) clearTimeout(timer);
      ws?.close(1000);
    };
  }, [jobId, tenantId, csrfToken]);

  return { mode, job, events };
}
