/** M5-1 管道拓扑画布：纯函数构图 + 渲染降级语义。 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { PipelineCanvas, buildPipelineGraph } from '../components/PipelineCanvas';

const PLAN = {
  pipeline_name: 'custom-fast',
  pipeline_revision: 2,
  pipelines: {
    pdf: {
      mode: 'first_success',
      steps: [
        { adapter: 'pdf-text-local', required: true },
        { adapter: 'ocr-local', required: false },
      ],
    },
    txt: {
      steps: [{ adapter: 'text-local' }],
    },
  },
};

describe('buildPipelineGraph（执行计划 → 图）', () => {
  it('按格式分行、步骤分列，边串成完整链路', () => {
    const { nodes, edges } = buildPipelineGraph(PLAN);
    const byId = Object.fromEntries(nodes.map((node) => [node.id, node]));

    expect(Object.keys(byId)).toEqual([
      'format:pdf',
      'step:pdf:0',
      'step:pdf:1',
      'output:pdf',
      'format:txt',
      'step:txt:0',
      'output:txt',
    ]);
    expect(byId['format:pdf']?.position).toEqual({ x: 0, y: 0 });
    expect(byId['format:txt']?.position).toEqual({ x: 0, y: 130 }); // 第二行
    expect(byId['step:pdf:0']?.data.label).toBe('pdf-text-local（必需）');
    expect(byId['step:pdf:1']?.data.label).toBe('ocr-local（可选）');
    expect(byId['output:pdf']?.data.label).toBe('IR · first_success');
    expect(byId['output:txt']?.data.label).toBe('IR · first_success'); // mode 缺省
    expect(edges.map((edge) => `${edge.source}->${edge.target}`)).toEqual([
      'format:pdf->step:pdf:0',
      'step:pdf:0->step:pdf:1',
      'step:pdf:1->output:pdf',
      'format:txt->step:txt:0',
      'step:txt:0->output:txt',
    ]);
  });

  it('坏数据降级为空图；无步骤的分支仍保留输入/输出节点', () => {
    expect(buildPipelineGraph(null)).toEqual({ nodes: [], edges: [] });
    expect(buildPipelineGraph({ pipelines: null })).toEqual({ nodes: [], edges: [] });
    const emptyBranch = buildPipelineGraph({ pipelines: { pdf: {} } });
    expect(emptyBranch.nodes.map((node) => node.id)).toEqual(['format:pdf', 'output:pdf']);
    expect(emptyBranch.edges).toHaveLength(1);
  });
});

describe('PipelineCanvas', () => {
  it('渲染画布与节点文本', () => {
    render(<PipelineCanvas plan={PLAN} />);
    expect(screen.getByTestId('pipeline-canvas')).toBeInTheDocument();
    expect(screen.getByText('格式 pdf')).toBeInTheDocument();
    expect(screen.getByText('pdf-text-local（必需）')).toBeInTheDocument();
  });

  it('空执行计划展示明确提示（不画空图）', () => {
    render(<PipelineCanvas plan={{ pipelines: {} }} />);
    expect(screen.getByText('执行计划不含 pipelines 分支。')).toBeInTheDocument();
    expect(screen.queryByTestId('pipeline-canvas')).not.toBeInTheDocument();
  });
});
