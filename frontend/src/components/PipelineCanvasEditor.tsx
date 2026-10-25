/**
 * 可编辑管道画布（M5-1）：从当前 YAML 草稿构建图，编辑实时回写 YAML。
 *
 * 设计口径：
 * - 管道 schema 是“每格式一条线性步骤链”，不支持任意 DAG；因此连线语义是
 *   “调整步骤先后”（从后置步骤连到前置步骤 = 移到它前面），而不是新增拓扑；
 * - 拖拽步骤节点按落点列重排；虚线＋节点在分支末尾追加步骤；
 * - 选中步骤后可改 adapter/必需并删除；`when` 条件仍由表单模式编辑（此处只提示数量）；
 * - 每次编辑都整体序列化 `pipelines` 段，保留顶层字段与条件（与表单模式同一模型）。
 */
import { useEffect, useId, useMemo, useState } from 'react';
import {
  Background,
  Controls,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import {
  parsePipelineForm,
  serializePipelineForm,
  type PipelineFormBranch,
  type PipelineFormStep,
} from '../lib/pipelineForm';

export const EDITOR_COLUMN_GAP = 200;
export const EDITOR_ROW_GAP = 140;

export interface EditableGraph {
  nodes: Node[];
  edges: Edge[];
}

export interface EditorNodeData {
  kind: 'format' | 'step' | 'output' | 'add';
  format: string;
  index?: number;
  label: string;
}

function moveStepTo(steps: PipelineFormStep[], from: number, to: number): PipelineFormStep[] {
  if (from === to || from < 0 || to < 0 || from >= steps.length || to >= steps.length) {
    return steps;
  }
  const next = steps.slice();
  const [item] = next.splice(from, 1);
  if (item) next.splice(to, 0, item);
  return next;
}

/** 拖拽落点 x → 目标步骤下标（吸附近列并夹取）。 */
export function resolveDropIndex(x: number, stepCount: number): number {
  if (stepCount <= 0) return 0;
  const raw = Math.round((x - EDITOR_COLUMN_GAP) / EDITOR_COLUMN_GAP);
  return Math.min(stepCount - 1, Math.max(0, raw));
}

/** 在指定格式分支内移动步骤；返回新分支数组（不改原数组）。 */
export function moveStepInBranches(
  branches: PipelineFormBranch[],
  format: string,
  from: number,
  to: number,
): PipelineFormBranch[] {
  const branch = branches.find((item) => item.format === format);
  if (!branch) return branches;
  const steps = moveStepTo(branch.steps, from, to);
  if (steps === branch.steps) return branches;
  return branches.map((item) => (item.format === format ? { ...item, steps } : item));
}

/**
 * 连线排序：仅同格式的 step→step 且 source 在后、target 在前时，
 * 把 source 移到 target 位置；其余情况视为无操作（返回原数组）。
 */
export function reorderByConnection(
  branches: PipelineFormBranch[],
  source: { format: string; index: number },
  target: { format: string; index: number },
): PipelineFormBranch[] {
  if (source.format !== target.format || source.index <= target.index) return branches;
  return moveStepInBranches(branches, source.format, source.index, target.index);
}

export function buildEditableGraph(branches: PipelineFormBranch[]): EditableGraph {
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  branches.forEach((branch, rowIndex) => {
    const y = rowIndex * EDITOR_ROW_GAP;
    const formatId = `format:${branch.format}`;
    nodes.push({
      id: formatId,
      type: 'input',
      position: { x: 0, y },
      draggable: false,
      connectable: false,
      data: {
        kind: 'format',
        format: branch.format,
        label: `格式 ${branch.format}`,
      } satisfies EditorNodeData,
    });
    let previous = formatId;
    branch.steps.forEach((step, index) => {
      const stepId = `step:${branch.format}:${index}`;
      const suffix = step.required ? '' : '（可选）';
      const conditions = step.conditions.length > 0 ? ` · ${step.conditions.length} 条条件` : '';
      nodes.push({
        id: stepId,
        position: { x: (index + 1) * EDITOR_COLUMN_GAP, y },
        data: {
          kind: 'step',
          format: branch.format,
          index,
          label: `${step.adapter || '未命名 adapter'}${suffix}${conditions}`,
        } satisfies EditorNodeData,
      });
      edges.push({ id: `edge:${previous}->${stepId}`, source: previous, target: stepId });
      previous = stepId;
    });
    const outputId = `output:${branch.format}`;
    nodes.push({
      id: outputId,
      type: 'output',
      position: { x: (branch.steps.length + 1) * EDITOR_COLUMN_GAP, y },
      draggable: false,
      connectable: false,
      data: {
        kind: 'output',
        format: branch.format,
        label: `IR · ${branch.mode}`,
      } satisfies EditorNodeData,
    });
    edges.push({ id: `edge:${previous}->${outputId}`, source: previous, target: outputId });
    nodes.push({
      id: `add:${branch.format}`,
      position: { x: (branch.steps.length + 2) * EDITOR_COLUMN_GAP, y },
      draggable: false,
      connectable: false,
      data: {
        kind: 'add',
        format: branch.format,
        label: '＋ 添加步骤',
      } satisfies EditorNodeData,
    });
  });

  return { nodes, edges };
}

export interface PipelineCanvasEditorProps {
  yamlText: string;
  adapters?: string[];
  onChange: (yamlText: string) => void;
  /** 条件编辑仍在表单模式：提供入口。 */
  onSwitchToForm?: () => void;
}

export function PipelineCanvasEditor({
  yamlText,
  adapters = [],
  onChange,
  onSwitchToForm,
}: PipelineCanvasEditorProps) {
  const listId = useId();
  const parsed = useMemo(() => parsePipelineForm(yamlText), [yamlText]);
  const branches = parsed.ok ? parsed.branches : [];
  const graph = useMemo(
    () => buildEditableGraph(parsed.ok ? parsed.branches : []),
    [parsed],
  );
  const [nodes, setNodes, onNodesChange] = useNodesState(graph.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(graph.edges);
  const [selected, setSelected] = useState<{ format: string; index: number } | null>(null);

  useEffect(() => {
    setNodes(graph.nodes);
    setEdges(graph.edges);
  }, [graph, setNodes, setEdges]);

  if (!parsed.ok) {
    return (
      <p role="alert" data-testid="canvas-editor-error" className="text-xs text-state-danger">
        {parsed.error}；请切回 YAML 模式修正后再使用画布。
      </p>
    );
  }

  const branchByFormat = new Map(branches.map((branch) => [branch.format, branch]));
  const selectedBranch = selected ? branchByFormat.get(selected.format) : undefined;
  const selectedStep =
    selectedBranch && selected ? selectedBranch.steps[selected.index] : undefined;

  const commit = (next: PipelineFormBranch[]) => onChange(serializePipelineForm(parsed.doc, next));
  const updateBranch = (
    format: string,
    updater: (branch: PipelineFormBranch) => PipelineFormBranch,
  ) => commit(branches.map((branch) => (branch.format === format ? updater(branch) : branch)));

  const nodeData = (nodeId: string): EditorNodeData | null => {
    const node = nodes.find((item) => item.id === nodeId);
    return node ? (node.data as unknown as EditorNodeData) : null;
  };

  const validConnection = (connection: Connection | Edge): boolean => {
    if (!connection.source || !connection.target) return false;
    const source = nodeData(connection.source);
    const target = nodeData(connection.target);
    if (!source || !target || source.kind !== 'step' || target.kind !== 'step') return false;
    return (
      source.format === target.format &&
      typeof source.index === 'number' &&
      typeof target.index === 'number' &&
      source.index > target.index
    );
  };

  const handleConnect = (connection: Connection) => {
    if (!connection.source || !connection.target) return;
    const source = nodeData(connection.source);
    const target = nodeData(connection.target);
    if (
      !source ||
      !target ||
      source.kind !== 'step' ||
      target.kind !== 'step' ||
      typeof source.index !== 'number' ||
      typeof target.index !== 'number'
    ) {
      return;
    }
    setSelected(null);
    commit(
      reorderByConnection(
        branches,
        { format: source.format, index: source.index },
        { format: target.format, index: target.index },
      ),
    );
  };

  const handleDragStop = (_event: unknown, node: Node) => {
    const data = node.data as unknown as EditorNodeData;
    if (data.kind !== 'step' || typeof data.index !== 'number') return;
    const branch = branchByFormat.get(data.format);
    if (!branch) return;
    const to = resolveDropIndex(node.position.x, branch.steps.length);
    if (to === data.index) {
      setNodes(graph.nodes); // 未改变顺序：吸附回原列
      return;
    }
    setSelected(null);
    commit(moveStepInBranches(branches, data.format, data.index, to));
  };

  const addStep = (format: string) => {
    setSelected(null);
    updateBranch(format, (branch) => ({
      ...branch,
      steps: [...branch.steps, { adapter: '', required: true, conditions: [] }],
    }));
  };

  const updateSelectedStep = (updater: (step: PipelineFormStep) => PipelineFormStep) => {
    if (!selected) return;
    updateBranch(selected.format, (branch) => ({
      ...branch,
      steps: branch.steps.map((step, index) => (index === selected.index ? updater(step) : step)),
    }));
  };

  return (
    <div className="space-y-2" data-testid="pipeline-canvas-editor">
      <p className="text-xs text-ink-muted">
        拖拽步骤节点调整顺序；从后置步骤连线到前置步骤可将其移到前面；虚线＋节点在分支末尾添加步骤。
        连线仅允许同一格式分支。
      </p>
      <datalist id={listId}>
        {adapters.map((name) => (
          <option key={name} value={name} />
        ))}
      </datalist>
      <div className="h-80 rounded-card border border-surface-border">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onNodeDragStop={handleDragStop}
          onConnect={handleConnect}
          isValidConnection={validConnection}
          onNodeClick={(_event, node) => {
            const data = node.data as unknown as EditorNodeData;
            if (data.kind === 'add') {
              addStep(data.format);
              return;
            }
            if (data.kind === 'step' && typeof data.index === 'number') {
              setSelected({ format: data.format, index: data.index });
            } else {
              setSelected(null);
            }
          }}
          onPaneClick={() => setSelected(null)}
          deleteKeyCode={null}
          fitView
          proOptions={{ hideAttribution: true }}
        >
          <Background />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>

      {selectedStep && selected && (
        <div
          data-testid="canvas-step-panel"
          className="flex flex-wrap items-center gap-2 rounded-card border border-surface-border p-2 text-xs"
        >
          <span className="text-ink-muted">
            {selected.format} · 步骤 {selected.index + 1}
          </span>
          <input
            aria-label="画布步骤 adapter"
            list={listId}
            value={selectedStep.adapter}
            onChange={(event) =>
              updateSelectedStep((step) => ({ ...step, adapter: event.target.value }))
            }
            placeholder="adapter 名称"
            className="w-52 rounded-btn border border-surface-border bg-surface px-2 py-1 font-mono"
          />
          <label className="flex items-center gap-1 text-ink-muted">
            <input
              type="checkbox"
              aria-label="画布步骤必需"
              checked={selectedStep.required}
              onChange={(event) =>
                updateSelectedStep((step) => ({ ...step, required: event.target.checked }))
              }
            />
            必需
          </label>
          <span className="text-ink-muted">
            {selectedStep.conditions.length > 0
              ? `${selectedStep.conditions.length} 条 when 条件`
              : '无 when 条件'}
          </span>
          {onSwitchToForm && selectedStep.conditions.length > 0 && (
            <button
              type="button"
              onClick={onSwitchToForm}
              className="rounded-tag border border-surface-border px-2 py-0.5 hover:bg-surface-muted"
            >
              在表单中编辑条件
            </button>
          )}
          <button
            type="button"
            onClick={() => {
              if (!selected) return;
              updateBranch(selected.format, (branch) => ({
                ...branch,
                steps: branch.steps.filter((_, index) => index !== selected.index),
              }));
              setSelected(null);
            }}
            className="rounded-tag border border-surface-border px-2 py-0.5 text-state-danger hover:bg-surface-muted"
          >
            删除步骤
          </button>
        </div>
      )}

      {branches.length === 0 && (
        <p className="text-xs text-state-warn">暂无格式分支，请切到 YAML/表单模式添加。</p>
      )}
    </div>
  );
}
