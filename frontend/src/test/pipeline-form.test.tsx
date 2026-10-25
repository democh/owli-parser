/** M5-1 表单模式：YAML ↔ 结构化模型的解析/序列化与交互。 */
import { fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { parse } from 'yaml';
import { PipelineForm } from '../components/PipelineForm';
import { parsePipelineForm, serializePipelineForm } from '../lib/pipelineForm';

const YAML = `schema_version: "1.0"
name: demo
revision: 3
pipelines:
  pdf:
    mode: first_success
    candidates:
      - adapter: pdf-text-local
        when:
          text_layer_ratio:
            gte: 0.5
      - adapter: pdf-ocr
        required: false
  docx:
    mode: sequence
    steps:
      - adapter: docx-local
`;

const ADAPTERS = ['pdf-text-local', 'pdf-ocr', 'docx-local'];

interface ParsedStep {
  adapter: string;
  required?: boolean;
  when?: Record<string, unknown>;
}

interface ParsedBranch {
  mode?: string;
  candidates?: ParsedStep[];
  steps?: ParsedStep[];
}

interface ParsedDoc {
  name?: string;
  revision?: number;
  pipelines: Record<string, ParsedBranch>;
}

function lastDoc(spy: ReturnType<typeof vi.fn>): ParsedDoc {
  const text = spy.mock.calls.at(-1)?.[0] as string;
  return parse(text) as ParsedDoc;
}

/** 受控组合：让表单的 onChange 回写真实驱动重渲染（覆盖多步编辑）。 */
function Harness() {
  const [text, setText] = useState(YAML);
  return (
    <>
      <PipelineForm yamlText={text} adapters={ADAPTERS} onChange={setText} />
      <textarea aria-label="result" readOnly value={text} />
    </>
  );
}

function resultDoc(): ParsedDoc {
  return parse((screen.getByLabelText('result') as HTMLTextAreaElement).value) as ParsedDoc;
}

describe('parsePipelineForm / serializePipelineForm', () => {
  it('解析分支（排序）并保留 when 与 required 语义', () => {
    const parsed = parsePipelineForm(YAML);
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    expect(parsed.branches.map((branch) => branch.format)).toEqual(['docx', 'pdf']);
    const pdf = parsed.branches.find((branch) => branch.format === 'pdf');
    expect(pdf?.mode).toBe('first_success');
    expect(pdf?.steps).toHaveLength(2);
    expect(pdf?.steps[0]).toMatchObject({ adapter: 'pdf-text-local', required: true });
    expect(pdf?.steps[0]?.conditions).toEqual([
      { feature: 'text_layer_ratio', operator: 'gte', valueText: '0.5' },
    ]);
    expect(pdf?.steps[1]).toMatchObject({ adapter: 'pdf-ocr', required: false });
    expect(pdf?.steps[1]?.conditions).toEqual([]);
    const docx = parsed.branches.find((branch) => branch.format === 'docx');
    expect(docx?.mode).toBe('sequence');
  });

  it('序列化保留顶层字段与 when，并可再次解析（往返稳定）', () => {
    const parsed = parsePipelineForm(YAML);
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    const text = serializePipelineForm(parsed.doc, parsed.branches);
    const doc = parse(text) as ParsedDoc;
    expect(doc.name).toBe('demo');
    expect(doc.revision).toBe(3);
    expect(doc.pipelines.pdf?.candidates?.[0]?.when).toEqual({ text_layer_ratio: { gte: 0.5 } });
    expect(doc.pipelines.docx?.steps).toEqual([{ adapter: 'docx-local', required: true }]);

    const roundTrip = parsePipelineForm(text);
    expect(roundTrip.ok).toBe(true);
    if (roundTrip.ok) expect(roundTrip.branches).toEqual(parsed.branches);
  });

  it('非法 YAML / 缺少 pipelines 返回错误而不抛异常', () => {
    const broken = parsePipelineForm('pipelines:\n  pdf: [\n');
    expect(broken.ok).toBe(false);
    const missing = parsePipelineForm('schema_version: "1.0"\n');
    expect(missing).toEqual({ ok: false, error: '缺少 pipelines 段（对象）' });
    const notObject = parsePipelineForm('- a\n- b\n');
    expect(notObject.ok).toBe(false);
  });

  it('畸形 when 条件返回错误（防止表单丢失路由语义）', () => {
    expect(
      parsePipelineForm(
        'pipelines:\n  pdf:\n    mode: first_success\n    candidates:\n      - adapter: x\n        when: nope\n',
      ),
    ).toEqual({ ok: false, error: 'pipelines.pdf 的步骤 1 when 必须是对象' });
    expect(
      parsePipelineForm(
        'pipelines:\n  pdf:\n    mode: first_success\n    candidates:\n      - adapter: x\n        when:\n          page_count:\n            regex: 3\n',
      ),
    ).toEqual({ ok: false, error: 'pipelines.pdf 的步骤 1 when.page_count 运算符 regex 不在白名单' });
    expect(
      parsePipelineForm(
        'pipelines:\n  pdf:\n    mode: first_success\n    candidates:\n      - adapter: x\n        when:\n          page_count:\n            gte: 1\n            lte: 3\n',
      ),
    ).toEqual({ ok: false, error: 'pipelines.pdf 的步骤 1 when.page_count 必须且只能包含一个运算符' });
  });

  it('畸形分支/步骤返回错误（避免表单回写时静默删数据）', () => {
    expect(parsePipelineForm('pipelines:\n  pdf: null\n')).toEqual({
      ok: false,
      error: 'pipelines.pdf 必须是对象',
    });
    expect(parsePipelineForm('pipelines:\n  pdf:\n    mode: priority\n')).toEqual({
      ok: false,
      error: 'pipelines.pdf.mode 仅支持 first_success/sequence',
    });
    expect(
      parsePipelineForm('pipelines:\n  pdf:\n    mode: first_success\n    candidates: nope\n'),
    ).toEqual({ ok: false, error: 'pipelines.pdf.candidates 必须是数组' });
    expect(
      parsePipelineForm('pipelines:\n  pdf:\n    mode: sequence\n    steps:\n      - nope\n'),
    ).toEqual({ ok: false, error: 'pipelines.pdf 的步骤必须是对象' });
  });
});

describe('PipelineForm when 条件', () => {
  it('编辑既有条件的运算符/值并按 JSON 类型回写', () => {
    render(<Harness />);
    fireEvent.change(screen.getByLabelText('pdf 步骤 1 条件 1 运算符'), {
      target: { value: 'lte' },
    });
    fireEvent.change(screen.getByLabelText('pdf 步骤 1 条件 1 值'), {
      target: { value: '0.25' },
    });
    expect(resultDoc().pipelines.pdf?.candidates?.[0]?.when).toEqual({
      text_layer_ratio: { lte: 0.25 },
    });
  });

  it('添加条件后填写特征即写入 when；删除条件后 when 消失', () => {
    render(<Harness />);
    fireEvent.click(screen.getByLabelText('为 pdf 步骤 2 添加条件'));
    // 未填特征前不写入 YAML（避免半成品条件）
    expect(resultDoc().pipelines.pdf?.candidates?.[1]?.when).toBeUndefined();

    fireEvent.change(screen.getByLabelText('pdf 步骤 2 条件 1 特征'), {
      target: { value: 'page_count' },
    });
    fireEvent.change(screen.getByLabelText('pdf 步骤 2 条件 1 值'), { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: '确认添加' }));
    expect(resultDoc().pipelines.pdf?.candidates?.[1]?.when).toEqual({ page_count: { gte: 3 } });

    fireEvent.click(screen.getByLabelText('删除 pdf 步骤 1 条件 1'));
    expect(resultDoc().pipelines.pdf?.candidates?.[0]?.when).toBeUndefined();
  });
});

describe('PipelineForm 步骤排序与校验', () => {
  it('拖拽步骤调整顺序并回写 YAML', () => {
    render(<Harness />);
    fireEvent.dragStart(screen.getByTestId('drag-pdf-1'));
    fireEvent.dragOver(screen.getByTestId('pipeline-step-pdf-2'));
    fireEvent.drop(screen.getByTestId('pipeline-step-pdf-2'));
    expect(resultDoc().pipelines.pdf?.candidates?.map((step) => step.adapter)).toEqual([
      'pdf-ocr',
      'pdf-text-local',
    ]);
  });

  it('同分支 adapter 重复时行内提示（保存前可发现）', () => {
    const duplicated = YAML.replace('adapter: pdf-ocr', 'adapter: pdf-text-local');
    render(<PipelineForm yamlText={duplicated} adapters={ADAPTERS} onChange={() => undefined} />);
    expect(screen.getByRole('status')).toHaveTextContent('adapter 重复：pdf-text-local');
  });
});

describe('PipelineForm 格式管理', () => {
  it('重命名格式保留模式与步骤', () => {
    render(<Harness />);
    fireEvent.click(screen.getByLabelText('重命名格式 pdf'));
    fireEvent.change(screen.getByLabelText('pdf 新格式名'), { target: { value: 'pdf2' } });
    fireEvent.click(screen.getByRole('button', { name: '确认重命名' }));
    const doc = resultDoc();
    expect(doc.pipelines.pdf).toBeUndefined();
    expect(doc.pipelines.pdf2?.mode).toBe('first_success');
    expect(doc.pipelines.pdf2?.candidates).toHaveLength(2);
  });

  it('空名或重名为已存在格式时禁用确认', () => {
    render(<Harness />);
    fireEvent.click(screen.getByLabelText('重命名格式 pdf'));
    const input = screen.getByLabelText('pdf 新格式名');
    fireEvent.change(input, { target: { value: '  ' } });
    expect(screen.getByRole('button', { name: '确认重命名' })).toBeDisabled();

    fireEvent.change(input, { target: { value: 'docx' } });
    expect(screen.getByRole('button', { name: '确认重命名' })).toBeDisabled();
    expect(screen.getByText('格式已存在')).toBeInTheDocument();
  });
});

describe('PipelineForm', () => {
  it('修改 adapter 实时回写 YAML，并保留 when 条件', () => {
    const onChange = vi.fn();
    render(<PipelineForm yamlText={YAML} adapters={ADAPTERS} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText('pdf 步骤 1 adapter'), {
      target: { value: 'pdf-text-v2' },
    });
    const pdf = lastDoc(onChange).pipelines.pdf;
    expect(pdf?.candidates?.[0]?.adapter).toBe('pdf-text-v2');
    expect(pdf?.candidates?.[0]?.when).toEqual({ text_layer_ratio: { gte: 0.5 } });
  });

  it('切换模式时在 candidates / steps 之间迁移步骤', () => {
    const onChange = vi.fn();
    render(<PipelineForm yamlText={YAML} adapters={ADAPTERS} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText('pdf 模式'), { target: { value: 'sequence' } });
    const pdf = lastDoc(onChange).pipelines.pdf;
    expect(pdf?.mode).toBe('sequence');
    expect(pdf?.candidates).toBeUndefined();
    expect(pdf?.steps?.map((step) => step.adapter)).toEqual(['pdf-text-local', 'pdf-ocr']);
  });

  it('上移/删除步骤与必需勾选按行生效', () => {
    const onChange = vi.fn();
    const { unmount } = render(
      <PipelineForm yamlText={YAML} adapters={ADAPTERS} onChange={onChange} />,
    );
    fireEvent.click(screen.getByLabelText('上移 pdf 步骤 2'));
    expect(lastDoc(onChange).pipelines.pdf?.candidates?.map((step) => step.adapter)).toEqual([
      'pdf-ocr',
      'pdf-text-local',
    ]);
    unmount();

    const remove = vi.fn();
    const second = render(<PipelineForm yamlText={YAML} adapters={ADAPTERS} onChange={remove} />);
    fireEvent.click(screen.getByLabelText('删除 pdf 步骤 1'));
    expect(lastDoc(remove).pipelines.pdf?.candidates).toHaveLength(1);
    second.unmount();

    const required = vi.fn();
    render(<PipelineForm yamlText={YAML} adapters={ADAPTERS} onChange={required} />);
    fireEvent.click(screen.getByLabelText('pdf 步骤 2 必需'));
    expect(lastDoc(required).pipelines.pdf?.candidates?.[1]?.required).toBe(true);
  });

  it('添加步骤与添加格式生成可继续编辑的 YAML', () => {
    const onChange = vi.fn();
    const first = render(<PipelineForm yamlText={YAML} adapters={ADAPTERS} onChange={onChange} />);
    fireEvent.click(screen.getByLabelText('为 pdf 添加步骤'));
    const pdf = lastDoc(onChange).pipelines.pdf;
    expect(pdf?.candidates).toHaveLength(3);
    expect(pdf?.candidates?.[2]).toEqual({ adapter: '', required: true });
    first.unmount();

    const addFormat = vi.fn();
    render(<PipelineForm yamlText={YAML} adapters={ADAPTERS} onChange={addFormat} />);
    fireEvent.change(screen.getByLabelText('新格式名称'), { target: { value: 'html' } });
    fireEvent.click(screen.getByRole('button', { name: '添加格式' }));
    expect(lastDoc(addFormat).pipelines.html).toEqual({ mode: 'first_success', candidates: [] });
  });

  it('删除格式后 YAML 中不再包含该分支', () => {
    const onChange = vi.fn();
    render(<PipelineForm yamlText={YAML} adapters={ADAPTERS} onChange={onChange} />);
    fireEvent.click(screen.getByLabelText('删除格式 docx'));
    expect(lastDoc(onChange).pipelines.docx).toBeUndefined();
    expect(lastDoc(onChange).pipelines.pdf).toBeDefined();
  });

  it('YAML 无法解析时展示错误且不提供半可信表单', () => {
    const onChange = vi.fn();
    render(<PipelineForm yamlText={'pipelines:\n  pdf: [\n'} adapters={ADAPTERS} onChange={onChange} />);
    expect(screen.getByTestId('pipeline-form-error')).toHaveTextContent('请切回 YAML 模式修正');
    expect(screen.queryByTestId('pipeline-form')).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });
});
