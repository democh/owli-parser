/** M5-1 可编辑画布：构图纯函数、拖拽/连线排序语义与交互。 */
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { parse } from 'yaml';
import {
  PipelineCanvasEditor,
  buildEditableGraph,
  moveStepInBranches,
  reorderByConnection,
  resolveDropIndex,
} from '../components/PipelineCanvasEditor';
import { parsePipelineForm, type PipelineFormBranch } from '../lib/pipelineForm';

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

interface ParsedDoc {
  pipelines: Record<
    string,
    {
      mode?: string;
      candidates?: Array<{ adapter: string; required?: boolean; when?: unknown }>;
      steps?: Array<{ adapter: string; required?: boolean; when?: unknown }>;
    }
  >;
}

function branches(): PipelineFormBranch[] {
  const parsed = parsePipelineForm(YAML);
  if (!parsed.ok) throw new Error(parsed.error);
  return parsed.branches;
}

function lastDoc(spy: ReturnType<typeof vi.fn>): ParsedDoc {
  return parse(spy.mock.calls.at(-1)?.[0] as string) as ParsedDoc;
}

describe('buildEditableGraph（YAML 草稿 → 可编辑图）', () => {
  it('按格式分行、步骤分列，含输出与添加节点', () => {
    const graph = buildEditableGraph(branches());
    const byId = Object.fromEntries(graph.nodes.map((node) => [node.id, node]));
    // 分支按格式名排序：docx 第一行、pdf 第二行
    expect(byId['format:docx']?.position).toEqual({ x: 0, y: 0 });
    expect(byId['add:docx']?.position).toEqual({ x: 600, y: 0 });
    expect(byId['format:pdf']?.position).toEqual({ x: 0, y: 140 });
    expect(byId['step:pdf:0']?.position).toEqual({ x: 200, y: 140 });
    expect(byId['step:pdf:1']?.position).toEqual({ x: 400, y: 140 });
    expect(byId['output:pdf']?.position).toEqual({ x: 600, y: 140 });
    expect(byId['add:pdf']?.position).toEqual({ x: 800, y: 140 });
    expect(byId['step:pdf:0']?.data.label).toBe('pdf-text-local · 1 条条件');
    // 非步骤节点不可拖拽/连线
    expect(byId['format:pdf']?.draggable).toBe(false);
    expect(byId['output:pdf']?.connectable).toBe(false);
    expect(graph.edges.map((edge) => `${edge.source}->${edge.target}`)).toEqual([
      'format:docx->step:docx:0',
      'step:docx:0->output:docx',
      'format:pdf->step:pdf:0',
      'step:pdf:0->step:pdf:1',
      'step:pdf:1->output:pdf',
    ]);
  });
});

describe('画布排序语义（纯函数）', () => {
  it('resolveDropIndex 按列吸附并夹取边界', () => {
    expect(resolveDropIndex(200, 3)).toBe(0);
    expect(resolveDropIndex(400, 3)).toBe(1);
    expect(resolveDropIndex(-100, 3)).toBe(0);
    expect(resolveDropIndex(9999, 3)).toBe(2);
    expect(resolveDropIndex(400, 0)).toBe(0);
  });

  it('moveStepInBranches 不变更其他分支，越界为 no-op', () => {
    const source = branches();
    const moved = moveStepInBranches(source, 'pdf', 0, 1);
    expect(moved.find((b) => b.format === 'pdf')?.steps.map((s) => s.adapter)).toEqual([
      'pdf-ocr',
      'pdf-text-local',
    ]);
    expect(moved.find((b) => b.format === 'docx')).toEqual(
      source.find((b) => b.format === 'docx'),
    );
    expect(moveStepInBranches(source, 'pdf', 0, 9)).toBe(source);
  });

  it('连线：仅同分支且后置→前置时移动；其余为 no-op', () => {
    const source = branches();
    const reordered = reorderByConnection(
      source,
      { format: 'pdf', index: 1 },
      { format: 'pdf', index: 0 },
    );
    expect(reordered.find((b) => b.format === 'pdf')?.steps.map((s) => s.adapter)).toEqual([
      'pdf-ocr',
      'pdf-text-local',
    ]);
    // 前置→后置 / 跨分支：不动
    expect(reorderByConnection(source, { format: 'pdf', index: 0 }, { format: 'pdf', index: 1 })).toBe(
      source,
    );
    expect(
      reorderByConnection(source, { format: 'pdf', index: 1 }, { format: 'docx', index: 0 }),
    ).toBe(source);
  });
});

describe('PipelineCanvasEditor 交互', () => {
  it('点击＋节点追加步骤并回写 YAML', () => {
    const onChange = vi.fn();
    render(<PipelineCanvasEditor yamlText={YAML} adapters={ADAPTERS} onChange={onChange} />);
    const addNode = document.querySelector('[data-id="add:pdf"]');
    expect(addNode).not.toBeNull();
    fireEvent.click(addNode!);
    const pdf = lastDoc(onChange).pipelines.pdf;
    expect(pdf?.candidates).toHaveLength(3);
    expect(pdf?.candidates?.[2]).toEqual({ adapter: '', required: true });
  });

  it('选中步骤可改 adapter/必需并删除；条件入口可切表单', () => {
    const onChange = vi.fn();
    const onSwitchToForm = vi.fn();
    render(
      <PipelineCanvasEditor
        yamlText={YAML}
        adapters={ADAPTERS}
        onChange={onChange}
        onSwitchToForm={onSwitchToForm}
      />,
    );
    fireEvent.click(screen.getByText('pdf-text-local · 1 条条件'));
    expect(screen.getByTestId('canvas-step-panel')).toHaveTextContent('1 条 when 条件');

    fireEvent.change(screen.getByLabelText('画布步骤 adapter'), {
      target: { value: 'pdf-v2' },
    });
    expect(lastDoc(onChange).pipelines.pdf?.candidates?.[0]?.adapter).toBe('pdf-v2');

    fireEvent.click(screen.getByRole('button', { name: '在表单中编辑条件' }));
    expect(onSwitchToForm).toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '删除步骤' }));
    expect(lastDoc(onChange).pipelines.pdf?.candidates).toHaveLength(1);
  });

  it('YAML 无法解析时展示错误而不渲染画布', () => {
    render(
      <PipelineCanvasEditor
        yamlText={'pipelines:\n  pdf: [\n'}
        adapters={ADAPTERS}
        onChange={() => undefined}
      />,
    );
    expect(screen.getByTestId('canvas-editor-error')).toHaveTextContent('请切回 YAML 模式修正');
    expect(screen.queryByTestId('pipeline-canvas-editor')).not.toBeInTheDocument();
  });
});
