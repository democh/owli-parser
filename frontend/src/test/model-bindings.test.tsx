/** 用途绑定编辑（纯组件）：已绑定摘要、默认档案、If-Match revision、预算三种语义。 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { ModelBinding, ModelProfile } from '../api/client';
import { ModelBindingsBoard } from '../components/ModelBindingsBoard';

const PROFILES: ModelProfile[] = [
  {
    id: 'p-gen',
    connection_id: 'c1',
    model_name: 'qwen2.5',
    revision: 1,
    capabilities: ['generation'],
    context_window: null,
    tokenizer_ref: null,
    max_input_tokens: null,
    max_output_tokens: null,
    supported_parameters: [],
    pricing_ref: null,
    embedding_dimensions: null,
    embedding_normalization: null,
    embedding_distance: null,
    output_kinds: [],
    state: 'published',
  },
  {
    id: 'p-emb',
    connection_id: 'c1',
    model_name: 'bge-m3',
    revision: 1,
    capabilities: ['embedding'],
    context_window: null,
    tokenizer_ref: null,
    max_input_tokens: null,
    max_output_tokens: null,
    supported_parameters: [],
    pricing_ref: null,
    embedding_dimensions: 1024,
    embedding_normalization: 'l2',
    embedding_distance: 'cosine',
    output_kinds: [],
    state: 'published',
  },
  {
    id: 'p-archived',
    connection_id: 'c1',
    model_name: 'old-model',
    revision: 1,
    capabilities: ['generation'],
    context_window: null,
    tokenizer_ref: null,
    max_input_tokens: null,
    max_output_tokens: null,
    supported_parameters: [],
    pricing_ref: null,
    embedding_dimensions: null,
    embedding_normalization: null,
    embedding_distance: null,
    output_kinds: [],
    state: 'archived',
  },
];

const BINDINGS: ModelBinding[] = [
  {
    id: 'b1',
    purpose: 'generation',
    primary_profile_id: 'p-gen',
    primary_model_name: 'qwen2.5',
    fallback_profile_ids: [],
    revision: 2,
    budget: { max_tokens_per_run: 500 },
  },
];

function renderBoard(overrides: Partial<Parameters<typeof ModelBindingsBoard>[0]> = {}) {
  return render(
    <ModelBindingsBoard
      profiles={PROFILES}
      bindings={BINDINGS}
      savingPurpose={null}
      error={null}
      onSave={() => undefined}
      {...overrides}
    />,
  );
}

describe('ModelBindingsBoard（用途绑定）', () => {
  it('已绑定展示模型/revision/预算；未绑定用途显示未绑定并预选可用档案', () => {
    renderBoard();
    const generation = screen.getByTestId('binding-generation');
    expect(generation).toHaveTextContent('qwen2.5 · r2');
    expect(generation).toHaveTextContent('500 tokens');
    expect(screen.getAllByText('未绑定')).toHaveLength(3);

    const embeddingSelect = screen.getByLabelText('向量（检索） 档案') as HTMLSelectElement;
    expect(embeddingSelect.value).toBe('p-emb');
    // 归档档案不出现在可选列表
    expect(within(embeddingSelect).queryByText(/old-model/)).not.toBeInTheDocument();
  });

  it('保存生成绑定：携带当前 revision 与预算', () => {
    const onSave = vi.fn();
    renderBoard({ onSave });
    fireEvent.change(screen.getByLabelText('生成预算 tokens'), { target: { value: '800' } });
    fireEvent.click(screen.getByRole('button', { name: '保存生成（问答）绑定' }));
    expect(onSave).toHaveBeenCalledWith({
      purpose: 'generation',
      primaryProfileId: 'p-gen',
      expectedRevision: 2,
      budget: { max_tokens_per_run: 800 },
    });
  });

  it('清空生成预算 → 显式空对象；非生成用途 → budget=null（不修改）', () => {
    const onSave = vi.fn();
    renderBoard({ onSave });
    fireEvent.change(screen.getByLabelText('生成预算 tokens'), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: '保存生成（问答）绑定' }));
    expect(onSave).toHaveBeenLastCalledWith({
      purpose: 'generation',
      primaryProfileId: 'p-gen',
      expectedRevision: 2,
      budget: {},
    });

    fireEvent.click(screen.getByRole('button', { name: '保存向量（检索）绑定' }));
    expect(onSave).toHaveBeenLastCalledWith({
      purpose: 'embedding',
      primaryProfileId: 'p-emb',
      expectedRevision: null,
      budget: null,
    });
  });

  it('保存中禁用对应按钮；错误以 alert 展示', () => {
    renderBoard({ savingPurpose: 'generation', error: '版本冲突（412）' });
    const button = screen.getByRole('button', { name: '保存生成（问答）绑定' });
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent('保存中…');
    expect(screen.getByTestId('binding-error')).toHaveTextContent('版本冲突');
  });
});
