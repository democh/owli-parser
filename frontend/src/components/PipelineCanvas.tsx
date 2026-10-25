/**
 * 管道执行计划画布（M5-1 只读拓扑）：格式分支 → adapter 步骤 → IR。
 *
 * - 输入是编译后的 execution_plan 快照（与 worker 读取的一致），不是 YAML 猜测；
 * - 自动布局按格式分行、按步骤分列；不可拖拽/连线，避免画出与快照不一致的图；
 * - 纯函数 buildPipelineGraph 独立测试，ReactFlow 仅负责渲染与缩放。
 */
import { useMemo } from 'react';
import { Background, Controls, ReactFlow, type Edge, type Node } from '@xyflow/react';
import '@xyflow/react/dist/style.css';

const COLUMN_GAP = 200;
const ROW_GAP = 130;

interface PlanStep {
  adapter?: unknown;
  required?: unknown;
}

interface PlanPipeline {
  mode?: unknown;
  steps?: unknown;
}

interface PlanShape {
  pipelines?: unknown;
}

export interface PipelineGraph {
  nodes: Node[];
  edges: Edge[];
}

/** execution_plan → 图节点/边（纯函数；坏数据降级为空图，不抛错）。 */
export function buildPipelineGraph(plan: Record<string, unknown> | null | undefined): PipelineGraph {
  const pipelines = (plan as PlanShape | null | undefined)?.pipelines;
  if (!pipelines || typeof pipelines !== 'object') return { nodes: [], edges: [] };

  const nodes: Node[] = [];
  const edges: Edge[] = [];
  const formats = Object.keys(pipelines as Record<string, unknown>).sort();

  formats.forEach((format, rowIndex) => {
    const pipeline = ((pipelines as Record<string, PlanPipeline>)[format] ?? {}) as PlanPipeline;
    const steps = Array.isArray(pipeline.steps) ? (pipeline.steps as PlanStep[]) : [];
    const y = rowIndex * ROW_GAP;
    const formatId = `format:${format}`;
    nodes.push({
      id: formatId,
      type: 'input',
      position: { x: 0, y },
      data: { label: `格式 ${format}` },
    });
    let previous = formatId;
    steps.forEach((step, stepIndex) => {
      const adapter = typeof step?.adapter === 'string' ? step.adapter : '未知 adapter';
      const required = step?.required === true;
      const stepId = `step:${format}:${stepIndex}`;
      nodes.push({
        id: stepId,
        position: { x: (stepIndex + 1) * COLUMN_GAP, y },
        data: { label: `${adapter}${required ? '（必需）' : '（可选）'}` },
      });
      edges.push({ id: `${previous}->${stepId}`, source: previous, target: stepId });
      previous = stepId;
    });
    const mode = typeof pipeline.mode === 'string' ? pipeline.mode : 'first_success';
    const outputId = `output:${format}`;
    nodes.push({
      id: outputId,
      type: 'output',
      position: { x: (steps.length + 1) * COLUMN_GAP, y },
      data: { label: `IR · ${mode}` },
    });
    edges.push({ id: `${previous}->${outputId}`, source: previous, target: outputId });
  });

  return { nodes, edges };
}

export function PipelineCanvas({ plan }: { plan: Record<string, unknown> | null | undefined }) {
  const graph = useMemo(() => buildPipelineGraph(plan), [plan]);
  if (graph.nodes.length === 0) {
    return <p className="text-xs text-ink-muted">执行计划不含 pipelines 分支。</p>;
  }
  return (
    <div className="h-64 rounded-card border border-surface-border" data-testid="pipeline-canvas">
      <ReactFlow
        nodes={graph.nodes}
        edges={graph.edges}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        fitView
        proOptions={{ hideAttribution: true }}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
