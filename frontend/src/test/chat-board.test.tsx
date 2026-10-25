/**
 * 知识问答（M4）测试：
 * - parseSseBuffer：跨 chunk 续拼、多事件、CRLF 与注释行；
 * - applyChatEvent：流式增量、证据定格、伪引用拒绝、拒答、seq 去重；
 * - splitAnswerCitations / citedNumbers：引用分段与实时高亮；
 * - ChatBoard：证据列表、[n] 锚点、失败/拒答展示、停止与续读回调；
 * - streamChatRun：fetch + ReadableStream 消费（头/体契约、[DONE] 结束、非 2xx 映射）。
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { parseSseBuffer, streamChatRun, type ChatEventItem, type ChatStreamEvent } from '../api/client';
import {
  ChatBoard,
  applyChatEvent,
  applyRunInfo,
  citedNumbers,
  createChatState,
  splitAnswerCitations,
  stateFromEvents,
  type ChatState,
} from '../pages/ChatPage';

const DATASETS = [{ id: 'ds-1', name: '产品知识库' }];

const EVIDENCE = {
  evidence_id: 'e1',
  chunk_id: 'aaaaaaaa-1111-2222-3333-444444444444',
  version_id: 'bbbbbbbb-1111-2222-3333-444444444444',
  chunk_revision: 3,
  text_preview: '差旅报销需在 30 日内提交。',
};

const FULL_EVENTS: Array<ChatEventItem & { run_id?: string }> = [
  { seq: 1, type: 'run.started', run_id: 'run-1', payload: { run_id: 'run-1', state: 'running' } },
  { seq: 2, type: 'retrieval.done', payload: { evidences: [EVIDENCE] } },
  { seq: 3, type: 'answer.delta', payload: { text: '需在 30 日内' } },
  { seq: 4, type: 'answer.delta', payload: { text: '提交[1]。' } },
  { seq: 5, type: 'answer.done', payload: { text: '需在 30 日内提交[1]。', citations: ['e1'] } },
];

function renderBoard(overrides: Partial<Parameters<typeof ChatBoard>[0]> = {}) {
  return render(
    <MemoryRouter>
      <ChatBoard
        datasets={DATASETS}
        state={createChatState()}
        busy={false}
        resuming={false}
        restored={false}
        cancelling={false}
        onAsk={() => undefined}
        onStop={() => undefined}
        onCancel={() => undefined}
        onResume={() => undefined}
        {...overrides}
      />
    </MemoryRouter>,
  );
}

describe('parseSseBuffer（SSE 分块解析）', () => {
  it('跨 chunk 保留残块并与后续数据续拼', () => {
    const first = parseSseBuffer('data: {"a":1}\n\ndata: {"b"');
    expect(first.events).toEqual(['{"a":1}']);
    expect(first.rest).toBe('data: {"b"');

    const second = parseSseBuffer(`${first.rest}:2}\n\n`);
    expect(second.events).toEqual(['{"b":2}']);
    expect(second.rest).toBe('');
  });

  it('兼容 CRLF 与注释/心跳行', () => {
    const crlf = String.fromCharCode(13, 10);
    const parsed = parseSseBuffer(`: ping${crlf}${crlf}data: [DONE]${crlf}${crlf}`);
    expect(parsed.events).toEqual(['[DONE]']);
  });

  it('多行 data 按行拼接（JSON 跨行不丢字）', () => {
    expect(parseSseBuffer('data: line1\ndata: line2\n\n').events).toEqual(['line1\nline2']);
  });
});

describe('applyChatEvent（事件归约）', () => {
  it('完整事件序列：证据、增量答案、引用与终态', () => {
    const state = stateFromEvents(FULL_EVENTS);
    expect(state.runId).toBe('run-1');
    expect(state.phase).toBe('done');
    expect(state.answer).toBe('需在 30 日内提交[1]。');
    expect(state.citations).toEqual(['e1']);
    expect(state.evidences).toHaveLength(1);
    expect(state.lastSeq).toBe(5);
  });

  it('seq 去重：重复投递同一事件不重复追加增量', () => {
    const partial = stateFromEvents(FULL_EVENTS.slice(0, 4));
    const again = stateFromEvents([...FULL_EVENTS.slice(0, 4), FULL_EVENTS[3]!]);
    expect(again.answer).toBe(partial.answer);
    expect(again.lastSeq).toBe(partial.lastSeq);
  });

  it('伪引用拒绝 → failed（携带越界编号）', () => {
    const state = stateFromEvents([
      { seq: 1, type: 'run.started', run_id: 'run-2', payload: {} },
      { seq: 2, type: 'retrieval.done', payload: { evidences: [EVIDENCE] } },
      { seq: 3, type: 'answer.delta', payload: { text: '见文献[9]。' } },
      {
        seq: 4,
        type: 'citation.rejected',
        payload: { invalid_citations: [9], evidence_count: 1, message: '答案引用了证据范围之外的编号，已拒绝' },
      },
    ]);
    expect(state.phase).toBe('failed');
    expect(state.error?.code).toBe('pseudo_citation');
    expect(state.error?.invalid_citations).toEqual([9]);
  });

  it('无证据拒答 → done + refusal（不进入生成）', () => {
    const state = stateFromEvents([
      { seq: 1, type: 'run.started', run_id: 'run-3', payload: {} },
      { seq: 2, type: 'retrieval.empty', payload: { message: '未检索到可用于回答的依据' } },
      { seq: 3, type: 'answer.refused', payload: { message: '资料中未找到依据，无法回答该问题。' } },
    ]);
    expect(state.phase).toBe('done');
    expect(state.refusal).toBe('资料中未找到依据，无法回答该问题。');
    expect(state.answer).toBe('');
  });

  it('run.failed：保留错误码与消息', () => {
    const state = stateFromEvents([
      { seq: 1, type: 'run.started', run_id: 'run-4', payload: {} },
      { seq: 2, type: 'run.failed', payload: { code: 'capability_not_ready', message: '生成未装配' } },
    ]);
    expect(state.phase).toBe('failed');
    expect(state.error).toEqual({ code: 'capability_not_ready', message: '生成未装配' });
  });

  it('run.cancelled：保留已送达增量，标记已取消且不产生完成答案', () => {
    const state = stateFromEvents([
      { seq: 1, type: 'run.started', run_id: 'run-6', payload: {} },
      { seq: 2, type: 'retrieval.done', payload: { evidences: [EVIDENCE] } },
      { seq: 3, type: 'answer.delta', payload: { text: '前半段' } },
      { seq: 4, type: 'run.cancelled', payload: { reason: 'client_requested' } },
    ]);
    expect(state.phase).toBe('cancelled');
    expect(state.answer).toBe('前半段');
    expect(state.error?.code).toBe('cancelled_by_request');
  });

  it('answer.done 记录 usage；run.failed 保留预算字段', () => {
    const withUsage = stateFromEvents([
      ...FULL_EVENTS.slice(0, 4),
      {
        seq: 5,
        type: 'answer.done',
        payload: {
          text: '需在 30 日内提交[1]。',
          citations: ['e1'],
          usage: { total_tokens: 30, source: 'provider' },
        },
      },
    ]);
    expect(withUsage.usage).toEqual({ total_tokens: 30, source: 'provider' });

    const budget = stateFromEvents([
      { seq: 1, type: 'run.started', run_id: 'run-budget', payload: {} },
      {
        seq: 2,
        type: 'run.failed',
        payload: {
          code: 'budget_exceeded',
          max_tokens: 100,
          used_tokens: 140,
          source: 'provider',
          message: '超预算',
        },
      },
    ]);
    expect(budget.phase).toBe('failed');
    expect(budget.error?.max_tokens).toBe(100);
    expect(budget.error?.source).toBe('provider');
  });

  it('applyRunInfo：服务端 cancelled 校正本地断流状态，并回填问题', () => {
    const partial = stateFromEvents(FULL_EVENTS.slice(0, 3));
    const corrected = applyRunInfo(partial, {
      id: 'run-1',
      state: 'cancelled',
      dataset_ids: [],
      request_snapshot: { question: '差旅报销多久内提交？' },
      error: { code: 'client_disconnected' },
    });
    expect(corrected.phase).toBe('cancelled');
    expect(corrected.question).toBe('差旅报销多久内提交？');
  });
});

describe('splitAnswerCitations（[n] 引用渲染）', () => {
  it('文本与引用分段，保留首尾文本', () => {
    expect(splitAnswerCitations('结论[1]，见[2]。')).toEqual([
      { kind: 'text', text: '结论' },
      { kind: 'citation', index: 1 },
      { kind: 'text', text: '，见' },
      { kind: 'citation', index: 2 },
      { kind: 'text', text: '。' },
    ]);
  });

  it('无引用/空答案安全', () => {
    expect(splitAnswerCitations('无引用')).toEqual([{ kind: 'text', text: '无引用' }]);
    expect(splitAnswerCitations('')).toEqual([]);
  });

  it('citedNumbers 提取去重编号', () => {
    expect([...citedNumbers('a[1]b[3]c[1]')]).toEqual([1, 3]);
  });
});

describe('ChatBoard（纯展示）', () => {
  it('初始空态提示，不显示答案与证据', () => {
    renderBoard();
    expect(screen.getByText('尚无问答记录')).toBeInTheDocument();
    expect(screen.queryByTestId('chat-answer')).not.toBeInTheDocument();
    expect(screen.queryByTestId('chat-evidences')).not.toBeInTheDocument();
  });

  it('答案中的 [n] 渲染为证据锚点，证据高亮并展示 revision', () => {
    renderBoard({ state: stateFromEvents(FULL_EVENTS) });
    expect(screen.getByTestId('chat-answer')).toHaveTextContent('需在 30 日内提交[1]。');
    const link = screen.getByRole('link', { name: '引用 e1' });
    expect(link).toHaveAttribute('href', '#evidence-e1');
    expect(document.getElementById('evidence-e1')).not.toBeNull();
    expect(screen.getByText('差旅报销需在 30 日内提交。')).toBeInTheDocument();
    expect(screen.getByText(/rev 3/)).toBeInTheDocument();
    expect(screen.getByTestId('chat-phase')).toHaveTextContent('完成');
  });

  it('伪引用失败展示越界编号', () => {
    const state = stateFromEvents([
      { seq: 1, type: 'run.started', run_id: 'run-2', payload: {} },
      { seq: 2, type: 'retrieval.done', payload: { evidences: [EVIDENCE] } },
      { seq: 3, type: 'citation.rejected', payload: { invalid_citations: [9] } },
    ]);
    renderBoard({ state });
    expect(screen.getByTestId('chat-error')).toHaveTextContent('越界编号 9');
  });

  it('生成未配置时提示去模型设置绑定 generation 用途', () => {
    const state = stateFromEvents([
      { seq: 1, type: 'run.started', run_id: 'run-5', payload: {} },
      { seq: 2, type: 'run.failed', payload: { code: 'capability_not_ready' } },
    ]);
    renderBoard({ state });
    expect(screen.getByTestId('chat-error')).toHaveTextContent('模型设置');
    expect(screen.getByTestId('chat-error')).toHaveTextContent('generation 用途绑定');
  });

  it('无证据拒答以警示文案展示', () => {
    renderBoard({
      state: stateFromEvents([
        { seq: 1, type: 'run.started', run_id: 'run-3', payload: {} },
        { seq: 2, type: 'answer.refused', payload: { message: '资料中未找到依据，无法回答该问题。' } },
      ]),
    });
    expect(screen.getByTestId('chat-refusal')).toHaveTextContent('资料中未找到依据');
  });

  it('提交回调携带问题与数据集；问题清空后禁用提交', () => {
    const onAsk = vi.fn();
    renderBoard({ onAsk });
    const submit = screen.getByRole('button', { name: '提问' });
    expect(submit).toBeDisabled();

    fireEvent.change(screen.getByLabelText('问题'), { target: { value: '  差旅报销多久内提交？  ' } });
    fireEvent.click(screen.getByRole('checkbox', { name: '产品知识库' }));
    expect(submit).not.toBeDisabled();
    fireEvent.click(submit);

    expect(onAsk).toHaveBeenCalledWith({
      question: '差旅报销多久内提交？',
      dataset_ids: ['ds-1'],
    });
    expect(screen.getByLabelText('问题')).toHaveValue('');
  });

  it('不选数据集也可提交（空数组 = 全部可访问数据集）', () => {
    const onAsk = vi.fn();
    renderBoard({ onAsk });
    fireEvent.change(screen.getByLabelText('问题'), { target: { value: '概括一下文档' } });
    fireEvent.click(screen.getByRole('button', { name: '提问' }));
    expect(onAsk).toHaveBeenCalledWith({ question: '概括一下文档', dataset_ids: [] });
  });

  it('生成中：断开连接回调（abort = 断流取消）', () => {
    const onStop = vi.fn();
    renderBoard({ state: { ...createChatState(), phase: 'streaming' }, busy: true, onStop });
    expect(screen.getByRole('button', { name: '生成中…' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '断开连接' }));
    expect(onStop).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('chat-phase')).toHaveTextContent('生成中');
    // 尚无 run_id 时不提供服务端取消
    expect(screen.queryByRole('button', { name: '取消生成' })).not.toBeInTheDocument();
  });

  it('生成中：显式取消回调；取消中按钮禁用', () => {
    const onCancel = vi.fn();
    const streaming = {
      ...createChatState(),
      runId: 'run-1',
      phase: 'streaming' as const,
    };
    const { unmount } = renderBoard({ state: streaming, busy: true, onCancel });
    fireEvent.click(screen.getByRole('button', { name: '取消生成' }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    unmount();

    renderBoard({ state: streaming, busy: true, cancelling: true, onCancel });
    expect(screen.getByRole('button', { name: '取消中…' })).toBeDisabled();
  });

  it('断流后可续读：回调携带最后序号', () => {
    const onResume = vi.fn();
    renderBoard({
      state: {
        ...stateFromEvents(FULL_EVENTS.slice(0, 4)),
        phase: 'failed',
        error: { code: 'stream_interrupted' },
      },
      onResume,
    });
    const resume = screen.getByRole('button', { name: '从 #4 续读事件' });
    fireEvent.click(resume);
    expect(onResume).toHaveBeenCalledWith(4);
    expect(screen.getByTestId('chat-error')).toHaveTextContent('事件流意外结束');
  });

  it('恢复提示仅在事件重放后出现', () => {
    renderBoard({ restored: true });
    expect(screen.getByTestId('chat-restored')).toBeInTheDocument();
  });

  it('多轮：展示会话历史与追问提示，新会话回调可清空', () => {
    const onNewConversation = vi.fn();
    renderBoard({
      thread: [{ runId: 'run-1', question: '第一个问题', answer: '第一轮回答[1]' }],
      followUp: true,
      onNewConversation,
    });
    expect(screen.getByTestId('chat-thread')).toHaveTextContent('第一个问题');
    expect(screen.getByTestId('chat-thread')).toHaveTextContent('第一轮回答[1]');
    expect(screen.getByTestId('chat-follow-up')).toHaveTextContent('追问');

    fireEvent.click(screen.getByRole('button', { name: '新会话' }));
    expect(onNewConversation).toHaveBeenCalledTimes(1);
  });

  it('生成中隐藏新会话按钮（避免中断当前会话）', () => {
    renderBoard({
      thread: [{ runId: 'run-1', question: '问题', answer: '回答' }],
      busy: true,
    });
    expect(screen.queryByRole('button', { name: '新会话' })).not.toBeInTheDocument();
  });

  it('展示本次用量（供应商返回/估算）', () => {
    renderBoard({
      state: {
        ...stateFromEvents(FULL_EVENTS),
        usage: { total_tokens: 30, source: 'provider' },
      },
    });
    expect(screen.getByTestId('chat-usage')).toHaveTextContent('30 tokens');
    expect(screen.getByTestId('chat-usage')).toHaveTextContent('供应商返回');
  });

  it('预算超限失败文案包含上限与拒绝语义', () => {
    const state = stateFromEvents([
      { seq: 1, type: 'run.started', run_id: 'run-budget', payload: {} },
      {
        seq: 2,
        type: 'run.failed',
        payload: { code: 'budget_exceeded', max_tokens: 100, message: '超出预算' },
      },
    ]);
    renderBoard({ state });
    expect(screen.getByTestId('chat-error')).toHaveTextContent('上限 100 tokens');
    expect(screen.getByTestId('chat-error')).toHaveTextContent('未产生完成答案');
  });
});

describe('streamChatRun（fetch + ReadableStream）', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function sseResponse(chunks: string[]): Response {
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    });
    return new Response(body, {
      status: 200,
      headers: { 'content-type': 'text/event-stream' },
    });
  }

  it('消费 data 行至 [DONE]；畸形行跳过且 [DONE] 后的数据不再投递', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      sseResponse([
        'data: {"seq":1,"type":"run.started","run_id":"r1","payload":{}}\n\n',
        'data: 这不是JSON\n\n',
        'data: {"seq":2,"type":"answer.delta","run_id":"r1","payload":{"text":"你',
        '好"}}\n\n',
        'data: [DONE]\n\n',
        'data: {"seq":3,"type":"answer.delta","run_id":"r1","payload":{"text":"不应到达"}}\n\n',
      ]),
    );
    vi.stubGlobal('fetch', fetchMock);

    const events: ChatStreamEvent[] = [];
    await streamChatRun({
      question: '差旅报销多久内提交？',
      datasetIds: ['ds-1'],
      parentRunId: 'run-parent',
      tenantId: 'tenant-1',
      csrfToken: 'csrf-1',
      onEvent: (event) => events.push(event),
    });

    expect(events.map((event) => event.type)).toEqual(['run.started', 'answer.delta']);
    expect(events[1]?.payload['text']).toBe('你好');

    const call = fetchMock.mock.calls[0]!;
    expect(String(call[0])).toBe('/v1/chat/runs');
    const init = call[1] as RequestInit;
    const headers = init.headers as Record<string, string>;
    expect(headers['X-Tenant-ID']).toBe('tenant-1');
    expect(headers['X-OWLI-CSRF']).toBe('csrf-1');
    expect(headers['Accept']).toBe('text/event-stream');
    expect(JSON.parse(String(init.body))).toEqual({
      question: '差旅报销多久内提交？',
      dataset_ids: ['ds-1'],
      parent_run_id: 'run-parent',
    });
  });

  it('非 2xx JSON 错误映射为 ApiError（含错误码）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            code: 'capability_not_ready',
            message: '生成未配置',
            detail: {},
            trace_id: 'trace-1',
          }),
          { status: 503, headers: { 'content-type': 'application/json' } },
        ),
      ),
    );
    await expect(
      streamChatRun({
        question: 'x',
        datasetIds: [],
        tenantId: 'tenant-1',
        onEvent: () => undefined,
      }),
    ).rejects.toMatchObject({ status: 503, code: 'capability_not_ready' });
  });

  it('无终态事件直接结束：调用方据 phase 判定中断（不伪造完成）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseResponse(['data: {"seq":1,"type":"run.started","run_id":"r9","payload":{}}\n\n']),
      ),
    );
    const events: ChatStreamEvent[] = [];
    await streamChatRun({
      question: 'x',
      datasetIds: [],
      tenantId: 'tenant-1',
      onEvent: (event) => events.push(event),
    });
    const state = events.reduce<ChatState>(
      (acc, event) => applyChatEvent(acc, event),
      createChatState(),
    );
    expect(state.phase).toBe('streaming');
  });
});
