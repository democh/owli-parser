/**
 * 知识问答（M4）：SSE 流式答案 + 证据列表 + [n] 引用定位 + 断流续读。
 *
 * 与后端一致的交互口径：
 * - 回答只依据 retrieval.done 冻结的证据；无证据时展示明确拒答（不编造）；
 * - [n] 是证据编号（e1..eN），越界即 run failed（pseudo_citation）；
 * - 停止 = 断开连接：服务端把运行标记为 cancelled（断开即取消，M4-2 前置口径）；
 * - 断流后可按 after_seq 续读；刷新页面按 localStorage 中最近 run_id 重放事件，
 *   历史答案不从可变 chunk 正文重建。
 *
 * ChatBoard 为纯展示组件（状态经 applyChatEvent 归约），便于单测覆盖
 * 流式、拒答、伪引用与断流恢复各状态。
 */
import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  ApiError,
  cancelChatRun,
  chatRun,
  chatRunEvents,
  request,
  streamChatRun,
  type ChatEventItem,
  type ChatEvidence,
  type ChatRunInfo,
  type ChatStreamEvent,
  type Dataset,
  type ListPage,
} from '../api/client';
import { Card, EmptyState } from '../components/shared';
import { useSessionStore } from '../stores/session';

export type ChatPhase = 'idle' | 'streaming' | 'done' | 'failed' | 'cancelled';

export interface ChatThreadTurn {
  runId: string;
  question: string;
  answer: string;
}

export interface ChatFailure {
  code: string;
  message?: string;
  invalid_citations?: number[];
  max_tokens?: number;
  used_tokens?: number;
  source?: string;
}

export interface ChatState {
  runId: string | null;
  phase: ChatPhase;
  question: string;
  evidences: ChatEvidence[];
  answer: string;
  citations: string[];
  refusal: string | null;
  error: ChatFailure | null;
  lastSeq: number;
  usage: Record<string, unknown> | null;
}

export function createChatState(): ChatState {
  return {
    runId: null,
    phase: 'idle',
    question: '',
    evidences: [],
    answer: '',
    citations: [],
    refusal: null,
    error: null,
    lastSeq: 0,
    usage: null,
  };
}

function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function asEvidences(value: unknown): ChatEvidence[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is ChatEvidence =>
      typeof item === 'object' && item !== null && typeof (item as ChatEvidence).evidence_id === 'string',
  );
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : null;
}

/** 单条事件 → 新状态（纯函数）。seq 单调去重：重放重复投递不叠加答案。 */
export function applyChatEvent(state: ChatState, event: ChatEventItem & { run_id?: string }): ChatState {
  if (event.seq <= state.lastSeq) return state;
  const payload = event.payload ?? {};
  const next: ChatState = { ...state, lastSeq: event.seq };
  switch (event.type) {
    case 'run.started':
      return { ...next, runId: event.run_id ?? state.runId, phase: 'streaming' };
    case 'retrieval.done':
      return { ...next, evidences: asEvidences(payload.evidences) };
    case 'answer.delta':
      return { ...next, answer: state.answer + asString(payload.text) };
    case 'answer.done':
      return {
        ...next,
        phase: 'done',
        answer: typeof payload.text === 'string' ? payload.text : state.answer,
        citations: Array.isArray(payload.citations) ? payload.citations.map(String) : [],
        usage: asRecord(payload.usage) ?? state.usage,
      };
    case 'answer.refused':
      return {
        ...next,
        phase: 'done',
        refusal: asString(payload.message, '资料中未找到依据，无法回答该问题。'),
      };
    case 'run.cancelled':
      return {
        ...next,
        phase: 'cancelled',
        error: {
          code: 'cancelled_by_request',
          message: asString(payload.message) || undefined,
        },
      };
    case 'citation.rejected':
      return {
        ...next,
        phase: 'failed',
        error: {
          code: 'pseudo_citation',
          message: asString(payload.message, '答案引用了证据范围之外的编号，已拒绝'),
          invalid_citations: Array.isArray(payload.invalid_citations)
            ? payload.invalid_citations.map(Number)
            : [],
        },
      };
    case 'run.failed': {
      const failure: ChatFailure = {
        code: asString(payload.code, 'execution_error'),
        message: asString(payload.message) || undefined,
      };
      if (typeof payload.max_tokens === 'number') failure.max_tokens = payload.max_tokens;
      if (typeof payload.used_tokens === 'number') failure.used_tokens = payload.used_tokens;
      if (typeof payload.source === 'string') failure.source = payload.source;
      return { ...next, phase: 'failed', error: failure };
    }
    default:
      return next;
  }
}

/** 事件重放（after_seq=0 或续读片段）：按 seq 顺序归约。 */
export function stateFromEvents(events: Array<ChatEventItem & { run_id?: string }>): ChatState {
  return events.reduce(applyChatEvent, createChatState());
}

/** 以服务端运行状态校正本地归约结果（取消/失败/流中断补齐终态）。 */
export function applyRunInfo(state: ChatState, run: ChatRunInfo): ChatState {
  const base: ChatState = {
    ...state,
    runId: run.id,
    question: state.question || asString(run.request_snapshot?.['question']),
    usage: state.usage ?? asRecord(run.request_snapshot?.['usage']),
  };
  if (run.state === 'cancelled' && base.phase !== 'done') {
    return { ...base, phase: 'cancelled' };
  }
  if (run.state === 'failed' && base.phase !== 'failed') {
    const error = run.error ?? {};
    return {
      ...base,
      phase: 'failed',
      error: {
        code: asString(error['code'], 'execution_error'),
        message: asString(error['message']) || undefined,
      },
    };
  }
  return base;
}

export type AnswerSegment =
  | { kind: 'text'; text: string }
  | { kind: 'citation'; index: number };

/** 答案拆分为文本与 [n] 引用段（引用渲染为可跳转到证据的锚点）。 */
export function splitAnswerCitations(answer: string): AnswerSegment[] {
  const segments: AnswerSegment[] = [];
  const pattern = /\[(\d+)\]/g;
  let last = 0;
  for (const match of answer.matchAll(pattern)) {
    const index = Number(match[1]);
    if (!Number.isFinite(index)) continue;
    const start = match.index ?? 0;
    if (start > last) segments.push({ kind: 'text', text: answer.slice(last, start) });
    segments.push({ kind: 'citation', index });
    last = start + match[0].length;
  }
  if (last < answer.length) segments.push({ kind: 'text', text: answer.slice(last) });
  return segments;
}

/** 引用编号集合（流式过程中即实时高亮对应证据）。 */
export function citedNumbers(answer: string): Set<number> {
  return new Set(
    splitAnswerCitations(answer)
      .filter((segment): segment is { kind: 'citation'; index: number } => segment.kind === 'citation')
      .map((segment) => segment.index),
  );
}

export function describeChatFailure(failure: ChatFailure): string {
  switch (failure.code) {
    case 'pseudo_citation': {
      const invalid = failure.invalid_citations?.join('、') ?? '';
      const suffix = invalid ? `（越界编号 ${invalid}）` : '';
      return `答案引用了证据范围之外的编号，已拒绝${suffix}`;
    }
    case 'capability_not_ready':
      return '生成模型未就绪：请在「模型设置」配置 generation 用途绑定（能力档案 + 连接与凭据）。';
    case 'retrieval_failed':
      return `证据检索失败${failure.message ? `：${failure.message}` : ''}`;
    case 'client_disconnected':
      return '连接已断开，运行已取消。';
    case 'cancelled_by_request':
      return '已按请求取消生成（已送达的增量保留，不会产生完成答案）。';
    case 'budget_exceeded': {
      const cap = failure.max_tokens !== undefined ? `（上限 ${failure.max_tokens} tokens）` : '';
      return `本次问答超出 token 预算${cap}，已中止且未产生完成答案。`;
    }
    case 'stream_interrupted':
      return '事件流意外结束：已展示部分答案，可从最后序号续读补齐。';
    default:
      return `运行失败（${failure.code}）${failure.message ? `：${failure.message}` : ''}`;
  }
}

function shortId(value: string): string {
  return value.length > 12 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
}

const PHASE_LABELS: Record<ChatPhase, string> = {
  idle: '未开始',
  streaming: '生成中',
  done: '完成',
  failed: '失败',
  cancelled: '已取消',
};

export interface ChatBoardProps {
  datasets: Array<{ id: string; name: string }>;
  state: ChatState;
  busy: boolean;
  resuming: boolean;
  restored: boolean;
  cancelling: boolean;
  thread?: ChatThreadTurn[];
  followUp?: boolean;
  onAsk: (payload: { question: string; dataset_ids: string[] }) => void;
  onStop: () => void;
  onCancel: () => void;
  onResume: (afterSeq: number) => void;
  onNewConversation?: () => void;
}

export function ChatBoard({
  datasets,
  state,
  busy,
  resuming,
  restored,
  cancelling,
  thread = [],
  followUp = false,
  onAsk,
  onStop,
  onCancel,
  onResume,
  onNewConversation = () => undefined,
}: ChatBoardProps) {
  const [question, setQuestion] = useState('');
  const [selected, setSelected] = useState<string[]>([]);
  const cited = citedNumbers(state.answer);
  const canAsk = !busy && question.trim().length > 0;
  const usageTotal = (() => {
    if (!state.usage) return null;
    const total = state.usage['total_tokens'];
    if (typeof total === 'number') return total;
    const estimated = state.usage['total_tokens_estimated'];
    return typeof estimated === 'number' ? estimated : null;
  })();

  const toggleDataset = (id: string) => {
    setSelected((prev) => (prev.includes(id) ? prev.filter((value) => value !== id) : [...prev, id]));
  };

  const submit = () => {
    if (!canAsk) return;
    onAsk({ question: question.trim(), dataset_ids: selected });
    setQuestion('');
  };

  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <div>
        <h1 className="text-lg font-semibold">知识问答</h1>
        <p className="mt-1 text-xs text-ink-muted">
          回答只依据检索到的证据，并以 [n] 标注引用编号（对应下方证据）；没有依据时会明确拒答，
          不会编造内容。停止或断开连接即取消本次运行。
        </p>
        {restored && (
          <p className="mt-1 text-xs text-ink-muted" data-testid="chat-restored">
            已从事件重放恢复最近一次运行（历史答案来自冻结快照）。
          </p>
        )}
      </div>

      <Card className="p-4">
        <div className="space-y-3">
          <div>
            <label className="text-xs text-ink-muted" htmlFor="chat-question">
              问题
            </label>
            <textarea
              id="chat-question"
              aria-label="问题"
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) submit();
              }}
              rows={3}
              maxLength={4000}
              placeholder="输入问题（Ctrl/⌘ + Enter 提交）"
              className="mt-1 w-full rounded-btn border border-surface-border bg-surface px-3 py-2 text-sm"
            />
          </div>
          <div>
            <div className="text-xs text-ink-muted">数据集范围（不选 = 全部可访问数据集）</div>
            <div className="mt-1 flex flex-wrap gap-2">
              {datasets.length === 0 && <span className="text-xs text-ink-muted">暂无数据集</span>}
              {datasets.map((dataset) => (
                <label
                  key={dataset.id}
                  className="flex items-center gap-1.5 rounded-tag border border-surface-border px-2 py-1 text-xs"
                >
                  <input
                    type="checkbox"
                    checked={selected.includes(dataset.id)}
                    onChange={() => toggleDataset(dataset.id)}
                  />
                  {dataset.name}
                </label>
              ))}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              disabled={!canAsk}
              onClick={submit}
              className="rounded-btn bg-brand px-4 py-2 text-sm text-white disabled:opacity-50"
            >
              {busy ? '生成中…' : '提问'}
            </button>
            {busy && (
              <>
                {state.runId && (
                  <button
                    type="button"
                    onClick={onCancel}
                    disabled={cancelling}
                    className="rounded-btn border border-surface-border px-3 py-2 text-xs hover:bg-surface-muted disabled:opacity-50"
                  >
                    {cancelling ? '取消中…' : '取消生成'}
                  </button>
                )}
                <button
                  type="button"
                  onClick={onStop}
                  className="rounded-btn border border-surface-border px-3 py-2 text-xs hover:bg-surface-muted"
                >
                  断开连接
                </button>
              </>
            )}
            <span className="text-xs text-ink-muted" data-testid="chat-phase">
              {PHASE_LABELS[state.phase]}
            </span>
            {followUp && (
              <span data-testid="chat-follow-up" className="text-xs text-ink-muted">
                追问：携带上一轮问答上下文
              </span>
            )}
            {state.runId && (
              <span className="font-mono text-xs text-ink-muted" title={state.runId}>
                run {shortId(state.runId)} · 已接收 #{state.lastSeq}
              </span>
            )}
          </div>
        </div>
      </Card>

      {thread.length > 0 && (
        <Card className="p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-xs text-ink-muted">会话历史（{thread.length} 轮）</div>
            {!busy && (
              <button
                type="button"
                onClick={onNewConversation}
                className="rounded-tag border border-surface-border px-2 py-0.5 text-xs hover:bg-surface-muted"
              >
                新会话
              </button>
            )}
          </div>
          <ol className="mt-2 space-y-2" data-testid="chat-thread">
            {thread.map((turn) => (
              <li key={turn.runId} className="rounded-card border border-surface-border p-2">
                <div className="text-xs text-ink-muted">问</div>
                <p className="whitespace-pre-wrap text-sm">{turn.question}</p>
                <div className="mt-1 text-xs text-ink-muted">答</div>
                <p className="whitespace-pre-wrap text-sm">{turn.answer}</p>
              </li>
            ))}
          </ol>
        </Card>
      )}

      {state.phase === 'idle' && !state.question && !state.refusal && !state.error && (
        <EmptyState
          title="尚无问答记录"
          hint="提出一个问题：回答只依据检索证据，并以 [n] 标注引用编号。"
        />
      )}

      {(state.question || state.answer || state.refusal || state.error) && (
        <Card className="p-4">
          {state.question && (
            <>
              <div className="text-xs text-ink-muted">问题</div>
              <p className="mt-1 whitespace-pre-wrap text-sm" data-testid="chat-question-echo">
                {state.question}
              </p>
            </>
          )}
          {state.refusal && (
            <p role="alert" className="mt-3 text-sm text-state-warn" data-testid="chat-refusal">
              {state.refusal}
            </p>
          )}
          {state.error && (
            <p role="alert" className="mt-3 text-sm text-state-danger" data-testid="chat-error">
              {describeChatFailure(state.error)}
            </p>
          )}
          {(state.answer || state.phase === 'streaming') && (
            <div className="mt-3">
              <div className="text-xs text-ink-muted">回答</div>
              <p
                className="mt-1 whitespace-pre-wrap text-sm leading-7"
                data-testid="chat-answer"
                aria-live="polite"
              >
                {splitAnswerCitations(state.answer).map((segment, index) =>
                  segment.kind === 'text' ? (
                    <span key={index}>{segment.text}</span>
                  ) : (
                    <a
                      key={index}
                      href={`#evidence-e${segment.index}`}
                      aria-label={`引用 e${segment.index}`}
                      className="mx-0.5 rounded-tag bg-brand-soft px-1 text-xs text-brand no-underline"
                    >
                      [{segment.index}]
                    </a>
                  ),
                )}
                {state.phase === 'streaming' && <span className="ml-0.5 animate-pulse">▍</span>}
              </p>
              {state.usage && (
                <p data-testid="chat-usage" className="mt-1 text-xs text-ink-muted">
                  本次用量：{usageTotal !== null ? `${usageTotal} tokens` : '—'}
                  {state.usage['source'] === 'estimate' ? '（估算）' : '（供应商返回）'}
                </p>
              )}
            </div>
          )}
          {state.runId && !busy && state.lastSeq > 0 && (
            <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-ink-muted">
              <button
                type="button"
                onClick={() => onResume(state.lastSeq)}
                disabled={resuming}
                className="rounded-tag border border-surface-border px-2 py-0.5 hover:bg-surface-muted disabled:opacity-50"
              >
                {resuming ? '续读中…' : `从 #${state.lastSeq} 续读事件`}
              </button>
              <span>断流后从最后序号继续，不重复已接收事件。</span>
            </div>
          )}
        </Card>
      )}

      {state.evidences.length > 0 && (
        <Card className="p-4">
          <div className="text-xs text-ink-muted">
            证据（{state.evidences.length} 条，编号与答案中的 [n] 对应）
          </div>
          <ol className="mt-2 space-y-3" data-testid="chat-evidences">
            {state.evidences.map((evidence, index) => {
              const reference = index + 1;
              const isCited = cited.has(reference);
              return (
                <li
                  key={evidence.evidence_id}
                  id={`evidence-${evidence.evidence_id}`}
                  className={`rounded-card border p-3 ${
                    isCited ? 'border-brand bg-brand-soft/30' : 'border-surface-border'
                  }`}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="rounded-tag bg-brand-soft px-2 py-0.5 text-xs text-brand">
                      [{reference}] {evidence.evidence_id}
                    </span>
                    <span
                      className="font-mono text-xs text-ink-muted"
                      title={`chunk ${evidence.chunk_id} · version ${evidence.version_id}`}
                    >
                      chunk {shortId(evidence.chunk_id)} · rev {evidence.chunk_revision}
                    </span>
                  </div>
                  <p className="mt-1 whitespace-pre-wrap text-sm">{evidence.text_preview}</p>
                </li>
              );
            })}
          </ol>
        </Card>
      )}
    </div>
  );
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

function lastRunKey(tenantId: string): string {
  return `owli.chat.lastRunId.${tenantId}`;
}

export function ChatPage() {
  const tenantId = useSessionStore((s) => s.currentTenantId);
  const csrfToken = useSessionStore((s) => s.csrfToken);
  const [state, setState] = useState<ChatState>(createChatState);
  const [busy, setBusy] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [restored, setRestored] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [thread, setThread] = useState<ChatThreadTurn[]>([]);
  const [parentRunId, setParentRunId] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const datasets = useQuery({
    queryKey: ['datasets', tenantId],
    enabled: Boolean(tenantId),
    queryFn: () => request<ListPage<Dataset>>('/datasets?limit=100', { tenantId }),
  });

  // 刷新/重进页面：按最近 run_id 从事件流恢复（答案来自冻结快照，不读可变 chunk）
  useEffect(() => {
    setState(createChatState());
    setThread([]);
    setParentRunId(null);
    setRestored(false);
    if (!tenantId) return;
    const runId = window.localStorage.getItem(lastRunKey(tenantId));
    if (!runId) return;
    let cancelled = false;
    void (async () => {
      try {
        const [info, events] = await Promise.all([
          chatRun(runId, tenantId),
          chatRunEvents({ runId, tenantId }),
        ]);
        if (cancelled || events.length === 0) return;
        const replayed = applyRunInfo(stateFromEvents(events), info);
        setState((prev) => (prev.phase === 'idle' && prev.lastSeq === 0 ? replayed : prev));
        setRestored(true);
        const terminal = events.find(
          (event) => event.type === 'answer.done' || event.type === 'answer.refused',
        );
        const question = asString(info.request_snapshot?.['question']);
        if (terminal && question) {
          const answer =
            terminal.type === 'answer.done'
              ? asString(terminal.payload['text'])
              : asString(terminal.payload['message']);
          if (answer) {
            setThread([{ runId, question, answer }]);
            setParentRunId(runId);
          }
        }
      } catch {
        // 运行不存在或已被清理：忽略恢复（保留空白会话）
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [tenantId]);

  async function ask(payload: { question: string; dataset_ids: string[] }) {
    if (!tenantId) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setBusy(true);
    setRestored(false);
    setCancelling(false);
    setState({ ...createChatState(), phase: 'streaming', question: payload.question });
    try {
      await streamChatRun({
        question: payload.question,
        datasetIds: payload.dataset_ids,
        parentRunId,
        tenantId,
        csrfToken,
        signal: controller.signal,
        onEvent: (event: ChatStreamEvent) => {
          if (event.run_id) window.localStorage.setItem(lastRunKey(tenantId), event.run_id);
          setState((prev) => applyChatEvent(prev, event));
          const runId = event.run_id;
          if (runId && (event.type === 'answer.done' || event.type === 'answer.refused')) {
            const answer =
              event.type === 'answer.done'
                ? asString(event.payload['text'])
                : asString(event.payload['message']);
            if (answer) {
              setThread((prev) =>
                prev.some((turn) => turn.runId === runId)
                  ? prev
                  : [...prev, { runId, question: payload.question, answer }],
              );
              setParentRunId(runId);
            }
          }
          if (event.type === 'run.failed' || event.type === 'run.cancelled') {
            setParentRunId(null);
          }
        },
      });
      setState((prev) =>
        prev.phase === 'streaming'
          ? {
              ...prev,
              phase: 'failed',
              error: { code: 'stream_interrupted', message: '事件流结束但未收到终态事件' },
            }
          : prev,
      );
    } catch (error) {
      setParentRunId(null);
      if (isAbortError(error)) {
        setState((prev) => ({ ...prev, phase: 'cancelled' }));
      } else {
        const failure: ChatFailure =
          error instanceof ApiError
            ? { code: error.code, message: error.message }
            : { code: 'stream_error', message: error instanceof Error ? error.message : String(error) };
        setState((prev) => ({ ...prev, phase: 'failed', error: failure }));
      }
    } finally {
      setBusy(false);
      abortRef.current = null;
    }
  }

  async function cancel() {
    const runId = state.runId;
    if (!tenantId || !runId || cancelling) return;
    setCancelling(true);
    try {
      await cancelChatRun(runId, tenantId, csrfToken);
      // 流仍会收到 run.cancelled 并正常结束；已送达增量保留
    } catch (error) {
      const failure: ChatFailure =
        error instanceof ApiError
          ? { code: error.code, message: error.message }
          : { code: 'cancel_failed', message: error instanceof Error ? error.message : String(error) };
      setState((prev) => ({ ...prev, error: failure }));
    } finally {
      setCancelling(false);
    }
  }

  async function resume(afterSeq: number) {
    const runId = state.runId;
    if (!tenantId || !runId) return;
    setResuming(true);
    try {
      const events = await chatRunEvents({ runId, tenantId, afterSeq });
      if (events.length > 0) {
        setState((prev) => events.reduce(applyChatEvent, prev));
      } else {
        // 无新增事件：以运行状态为准（例如断开后服务端已标记 cancelled）
        const info = await chatRun(runId, tenantId);
        setState((prev) => applyRunInfo(prev, info));
      }
    } catch (error) {
      const failure: ChatFailure =
        error instanceof ApiError
          ? { code: error.code, message: error.message }
          : { code: 'stream_error', message: error instanceof Error ? error.message : String(error) };
      setState((prev) => ({ ...prev, error: failure }));
    } finally {
      setResuming(false);
    }
  }

  function newConversation() {
    if (busy || state.phase === 'streaming') return;
    if (tenantId) window.localStorage.removeItem(lastRunKey(tenantId));
    setThread([]);
    setParentRunId(null);
    setState(createChatState());
    setRestored(false);
    setCancelling(false);
  }

  return (
    <ChatBoard
      datasets={(datasets.data?.items ?? []).map((dataset) => ({ id: dataset.id, name: dataset.name }))}
      state={state}
      busy={busy || state.phase === 'streaming'}
      resuming={resuming}
      restored={restored}
      cancelling={cancelling}
      thread={thread}
      followUp={Boolean(parentRunId)}
      onAsk={(payload) => void ask(payload)}
      onStop={() => abortRef.current?.abort()}
      onCancel={() => void cancel()}
      onResume={(afterSeq) => void resume(afterSeq)}
      onNewConversation={newConversation}
    />
  );
}
