/**
 * 管道表单模式（M5-1）：结构化编辑 pipelines 段，实时回写 YAML。
 *
 * - 受控组件：每次修改序列化为 YAML 交给上层草稿，校验/保存在上层完成；
 * - `when` 条件可按 feature/运算符/值编辑；值按 JSON 解析，裸文本按字符串；
 * - YAML 无法解析时降级为错误提示，不提供半可信表单。
 */
import { useId, useMemo, useState } from 'react';
import {
  parsePipelineForm,
  serializePipelineForm,
  WHEN_OPERATORS,
  type PipelineFormBranch,
  type PipelineFormStep,
  type WhenConditionForm,
  type WhenOperator,
} from '../lib/pipelineForm';

export interface PipelineFormProps {
  yamlText: string;
  /** 可选 adapter 名（来自 /adapters；未知值仍可手动输入，由服务端校验）。 */
  adapters?: string[];
  onChange: (yamlText: string) => void;
}

/** ProbeFeatures 字段（后端条件评估读取的属性）。 */
const FEATURE_SUGGESTIONS = [
  'text_layer_ratio',
  'page_count',
  'extension',
  'mime',
  'size_bytes',
  'encrypted',
  'has_zip_container',
  'language_hint',
];

function moveStep(steps: PipelineFormStep[], index: number, delta: number): PipelineFormStep[] {
  return moveStepTo(steps, index, index + delta);
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

/** 同分支内重复的 adapter（后端会拒绝；表单内提前提示）。 */
function duplicateAdapters(steps: PipelineFormStep[]): string[] {
  const seen = new Set<string>();
  const duplicates = new Set<string>();
  for (const step of steps) {
    const name = step.adapter.trim();
    if (!name) continue;
    if (seen.has(name)) duplicates.add(name);
    else seen.add(name);
  }
  return [...duplicates];
}

export function PipelineForm({ yamlText, adapters = [], onChange }: PipelineFormProps) {
  const listId = useId();
  const featureListId = useId();
  const parsed = useMemo(() => parsePipelineForm(yamlText), [yamlText]);
  const [newFormat, setNewFormat] = useState('');
  // 新条件先在本地草案中编辑，确认后才写入 YAML（避免空 feature 被序列化丢弃）
  const [pendingCondition, setPendingCondition] = useState<{
    format: string;
    stepIndex: number;
    condition: WhenConditionForm;
  } | null>(null);
  const [renaming, setRenaming] = useState<{ format: string; next: string } | null>(null);
  const [dragStep, setDragStep] = useState<{ format: string; index: number } | null>(null);
  const [dropStep, setDropStep] = useState<{ format: string; index: number } | null>(null);

  if (!parsed.ok) {
    return (
      <p
        role="alert"
        data-testid="pipeline-form-error"
        className="rounded-card border border-state-danger/40 bg-surface p-3 text-xs text-state-danger"
      >
        {parsed.error}；请切回 YAML 模式修正后再使用表单。
      </p>
    );
  }

  const { doc, branches } = parsed;
  const emit = (next: PipelineFormBranch[]) => onChange(serializePipelineForm(doc, next));
  const updateBranch = (format: string, updater: (branch: PipelineFormBranch) => PipelineFormBranch) =>
    emit(branches.map((branch) => (branch.format === format ? updater(branch) : branch)));
  const updateSteps = (format: string, steps: PipelineFormStep[]) =>
    updateBranch(format, (branch) => ({ ...branch, steps }));

  const addFormat = () => {
    const format = newFormat.trim();
    if (!format || branches.some((branch) => branch.format === format)) return;
    emit([...branches, { format, mode: 'first_success', steps: [] }]);
    setNewFormat('');
  };

  const optionNames = [...new Set([...adapters, ...branches.flatMap((b) => b.steps.map((s) => s.adapter))])]
    .filter((name) => name.length > 0)
    .sort();
  const duplicateFormat = Boolean(newFormat.trim()) && branches.some((b) => b.format === newFormat.trim());

  return (
    <div className="space-y-2" data-testid="pipeline-form">
      <p className="text-xs text-ink-muted">
        表单只编辑 `pipelines` 段；保存会重新序列化 YAML（注释与排版规范化）。
        `when` 值按 JSON 解析（如 0.5 / true / "zh" / ["a"]），裸文本按字符串。
      </p>
      <datalist id={listId}>
        {optionNames.map((name) => (
          <option key={name} value={name} />
        ))}
      </datalist>
      <datalist id={featureListId}>
        {FEATURE_SUGGESTIONS.map((feature) => (
          <option key={feature} value={feature} />
        ))}
      </datalist>

      {branches.map((branch) => (
        <div
          key={branch.format}
          data-testid={`pipeline-form-${branch.format}`}
          className="rounded-card border border-surface-border p-2"
        >
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-medium">{branch.format}</span>
            <select
              aria-label={`${branch.format} 模式`}
              value={branch.mode}
              onChange={(event) =>
                updateBranch(branch.format, (current) => ({
                  ...current,
                  mode: event.target.value === 'sequence' ? 'sequence' : 'first_success',
                }))
              }
              className="rounded-btn border border-surface-border bg-surface px-2 py-1 text-xs"
            >
              <option value="first_success">first_success（优先降级）</option>
              <option value="sequence">sequence（顺序增强）</option>
            </select>
            <button
              type="button"
              aria-label={`重命名格式 ${branch.format}`}
              onClick={() => setRenaming({ format: branch.format, next: branch.format })}
              className="rounded-tag border border-surface-border px-2 py-0.5 text-xs hover:bg-surface-muted"
            >
              重命名
            </button>
            <button
              type="button"
              aria-label={`删除格式 ${branch.format}`}
              onClick={() => emit(branches.filter((item) => item.format !== branch.format))}
              className="rounded-tag border border-surface-border px-2 py-0.5 text-xs hover:bg-surface-muted"
            >
              删除格式
            </button>
          </div>

          {renaming?.format === branch.format && (
            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
              <input
                aria-label={`${branch.format} 新格式名`}
                value={renaming.next}
                onChange={(event) => setRenaming({ ...renaming, next: event.target.value })}
                className="w-40 rounded-btn border border-surface-border bg-surface px-2 py-1 font-mono"
              />
              <button
                type="button"
                disabled={
                  !renaming.next.trim() ||
                  branches.some(
                    (item) => item.format === renaming.next.trim() && item.format !== branch.format,
                  )
                }
                onClick={() => {
                  const next = renaming.next.trim();
                  emit(
                    branches.map((item) =>
                      item.format === branch.format ? { ...item, format: next } : item,
                    ),
                  );
                  setRenaming(null);
                }}
                className="rounded-btn border border-surface-border px-2 py-1 hover:bg-surface-muted disabled:opacity-50"
              >
                确认重命名
              </button>
              <button
                type="button"
                onClick={() => setRenaming(null)}
                className="rounded-tag border border-surface-border px-2 py-0.5 hover:bg-surface-muted"
              >
                取消
              </button>
              {renaming.next.trim() !== branch.format &&
                branches.some((item) => item.format === renaming.next.trim()) && (
                  <span className="text-state-warn">格式已存在</span>
                )}
            </div>
          )}

          <div className="mt-2 space-y-1">
            {duplicateAdapters(branch.steps).map((name) => (
              <p key={name} role="status" className="text-xs text-state-warn">
                adapter 重复：{name}（保存前需去掉重复项）
              </p>
            ))}
            {branch.steps.map((step, index) => {
              const setConditions = (conditions: WhenConditionForm[]) =>
                updateSteps(
                  branch.format,
                  branch.steps.map((item, itemIndex) =>
                    itemIndex === index ? { ...item, conditions } : item,
                  ),
                );
              return (
              <div
                key={`${branch.format}-${index}`}
                data-testid={`pipeline-step-${branch.format}-${index + 1}`}
                onDragOver={(event) => {
                  if (dragStep?.format === branch.format) {
                    event.preventDefault();
                    setDropStep({ format: branch.format, index });
                  }
                }}
                onDrop={(event) => {
                  event.preventDefault();
                  if (dragStep?.format === branch.format) {
                    updateSteps(branch.format, moveStepTo(branch.steps, dragStep.index, index));
                  }
                  setDragStep(null);
                  setDropStep(null);
                }}
                className={`rounded-card border p-1.5 ${
                  dragStep?.format === branch.format &&
                  dropStep?.format === branch.format &&
                  dropStep.index === index &&
                  dragStep.index !== index
                    ? 'border-brand'
                    : 'border-surface-border/60'
                }`}
              >
                <div className="flex flex-wrap items-center gap-1.5 text-xs">
                <span
                  draggable
                  onDragStart={() => setDragStep({ format: branch.format, index })}
                  onDragEnd={() => {
                    setDragStep(null);
                    setDropStep(null);
                  }}
                  data-testid={`drag-${branch.format}-${index + 1}`}
                  title="拖拽调整顺序"
                  aria-hidden="true"
                  className="cursor-grab select-none px-1 text-ink-muted"
                >
                  ⠿
                </span>
                <span className="w-5 text-right text-ink-muted tabular-nums">{index + 1}.</span>
                <input
                  aria-label={`${branch.format} 步骤 ${index + 1} adapter`}
                  list={listId}
                  value={step.adapter}
                  onChange={(event) =>
                    updateSteps(
                      branch.format,
                      branch.steps.map((item, itemIndex) =>
                        itemIndex === index ? { ...item, adapter: event.target.value } : item,
                      ),
                    )
                  }
                  placeholder="adapter 名称"
                  className="w-52 rounded-btn border border-surface-border bg-surface px-2 py-1 font-mono"
                />
                <label className="flex items-center gap-1 text-ink-muted">
                  <input
                    type="checkbox"
                    aria-label={`${branch.format} 步骤 ${index + 1} 必需`}
                    checked={step.required}
                    onChange={(event) =>
                      updateSteps(
                        branch.format,
                        branch.steps.map((item, itemIndex) =>
                          itemIndex === index ? { ...item, required: event.target.checked } : item,
                        ),
                      )
                    }
                  />
                  必需
                </label>
                {step.conditions.length > 0 && (
                  <span className="rounded-tag bg-surface-muted px-1.5 py-0.5 text-ink-muted">
                    {step.conditions.length} 个 when 条件
                  </span>
                )}
                <button
                  type="button"
                  aria-label={`上移 ${branch.format} 步骤 ${index + 1}`}
                  disabled={index === 0}
                  onClick={() => updateSteps(branch.format, moveStep(branch.steps, index, -1))}
                  className="rounded-tag border border-surface-border px-1.5 py-0.5 hover:bg-surface-muted disabled:opacity-40"
                >
                  上移
                </button>
                <button
                  type="button"
                  aria-label={`下移 ${branch.format} 步骤 ${index + 1}`}
                  disabled={index === branch.steps.length - 1}
                  onClick={() => updateSteps(branch.format, moveStep(branch.steps, index, 1))}
                  className="rounded-tag border border-surface-border px-1.5 py-0.5 hover:bg-surface-muted disabled:opacity-40"
                >
                  下移
                </button>
                <button
                  type="button"
                  aria-label={`删除 ${branch.format} 步骤 ${index + 1}`}
                  onClick={() =>
                    updateSteps(
                      branch.format,
                      branch.steps.filter((_, itemIndex) => itemIndex !== index),
                    )
                  }
                  className="rounded-tag border border-surface-border px-1.5 py-0.5 text-state-danger hover:bg-surface-muted"
                >
                  删除
                </button>
                </div>

                <div className="mt-1 space-y-1 pl-6">
                  {step.conditions.map((condition, conditionIndex) => (
                    <div
                      key={conditionIndex}
                      className="flex flex-wrap items-center gap-1 text-xs"
                    >
                      <input
                        aria-label={`${branch.format} 步骤 ${index + 1} 条件 ${conditionIndex + 1} 特征`}
                        list={featureListId}
                        value={condition.feature}
                        onChange={(event) =>
                          setConditions(
                            step.conditions.map((item, itemIndex) =>
                              itemIndex === conditionIndex
                                ? { ...item, feature: event.target.value }
                                : item,
                            ),
                          )
                        }
                        placeholder="feature（如 text_layer_ratio）"
                        className="w-44 rounded-btn border border-surface-border bg-surface px-2 py-1 font-mono"
                      />
                      <select
                        aria-label={`${branch.format} 步骤 ${index + 1} 条件 ${conditionIndex + 1} 运算符`}
                        value={condition.operator}
                        onChange={(event) =>
                          setConditions(
                            step.conditions.map((item, itemIndex) =>
                              itemIndex === conditionIndex
                                ? { ...item, operator: event.target.value as WhenOperator }
                                : item,
                            ),
                          )
                        }
                        className="rounded-btn border border-surface-border bg-surface px-1 py-1 font-mono"
                      >
                        {WHEN_OPERATORS.map((operator) => (
                          <option key={operator} value={operator}>
                            {operator}
                          </option>
                        ))}
                      </select>
                      <input
                        aria-label={`${branch.format} 步骤 ${index + 1} 条件 ${conditionIndex + 1} 值`}
                        value={condition.valueText}
                        onChange={(event) =>
                          setConditions(
                            step.conditions.map((item, itemIndex) =>
                              itemIndex === conditionIndex
                                ? { ...item, valueText: event.target.value }
                                : item,
                            ),
                          )
                        }
                        placeholder="值（JSON）"
                        className="w-32 rounded-btn border border-surface-border bg-surface px-2 py-1 font-mono"
                      />
                      <button
                        type="button"
                        aria-label={`删除 ${branch.format} 步骤 ${index + 1} 条件 ${conditionIndex + 1}`}
                        onClick={() =>
                          setConditions(
                            step.conditions.filter((_, itemIndex) => itemIndex !== conditionIndex),
                          )
                        }
                        className="rounded-tag border border-surface-border px-1.5 py-0.5 text-state-danger hover:bg-surface-muted"
                      >
                        删除条件
                      </button>
                    </div>
                  ))}
                  {pendingCondition?.format === branch.format &&
                    pendingCondition.stepIndex === index && (
                      <div
                        className="flex flex-wrap items-center gap-1 rounded-card border border-dashed border-surface-border p-1 text-xs"
                        data-testid={`pending-condition-${branch.format}-${index + 1}`}
                      >
                        <input
                          aria-label={`${branch.format} 步骤 ${index + 1} 条件 ${step.conditions.length + 1} 特征`}
                          list={featureListId}
                          value={pendingCondition.condition.feature}
                          onChange={(event) =>
                            setPendingCondition({
                              ...pendingCondition,
                              condition: {
                                ...pendingCondition.condition,
                                feature: event.target.value,
                              },
                            })
                          }
                          placeholder="feature（必填）"
                          className="w-44 rounded-btn border border-surface-border bg-surface px-2 py-1 font-mono"
                        />
                        <select
                          aria-label={`${branch.format} 步骤 ${index + 1} 条件 ${step.conditions.length + 1} 运算符`}
                          value={pendingCondition.condition.operator}
                          onChange={(event) =>
                            setPendingCondition({
                              ...pendingCondition,
                              condition: {
                                ...pendingCondition.condition,
                                operator: event.target.value as WhenOperator,
                              },
                            })
                          }
                          className="rounded-btn border border-surface-border bg-surface px-1 py-1 font-mono"
                        >
                          {WHEN_OPERATORS.map((operator) => (
                            <option key={operator} value={operator}>
                              {operator}
                            </option>
                          ))}
                        </select>
                        <input
                          aria-label={`${branch.format} 步骤 ${index + 1} 条件 ${step.conditions.length + 1} 值`}
                          value={pendingCondition.condition.valueText}
                          onChange={(event) =>
                            setPendingCondition({
                              ...pendingCondition,
                              condition: {
                                ...pendingCondition.condition,
                                valueText: event.target.value,
                              },
                            })
                          }
                          placeholder="值（JSON）"
                          className="w-32 rounded-btn border border-surface-border bg-surface px-2 py-1 font-mono"
                        />
                        <button
                          type="button"
                          disabled={!pendingCondition.condition.feature.trim()}
                          onClick={() => {
                            setConditions([...step.conditions, pendingCondition.condition]);
                            setPendingCondition(null);
                          }}
                          className="rounded-tag border border-surface-border px-1.5 py-0.5 text-state-ok hover:bg-surface-muted disabled:opacity-40"
                        >
                          确认添加
                        </button>
                        <button
                          type="button"
                          onClick={() => setPendingCondition(null)}
                          className="rounded-tag border border-surface-border px-1.5 py-0.5 hover:bg-surface-muted"
                        >
                          取消
                        </button>
                      </div>
                    )}
                  <button
                    type="button"
                    aria-label={`为 ${branch.format} 步骤 ${index + 1} 添加条件`}
                    onClick={() =>
                      setPendingCondition({
                        format: branch.format,
                        stepIndex: index,
                        condition: { feature: '', operator: 'gte', valueText: '0.5' },
                      })
                    }
                    className="rounded-tag border border-surface-border px-2 py-0.5 text-xs hover:bg-surface-muted"
                  >
                    + when 条件
                  </button>
                </div>
              </div>
              );
            })}
            {branch.steps.length === 0 && (
              <p className="text-xs text-state-warn">该格式暂无步骤，保存前需补齐。</p>
            )}
            <button
              type="button"
              aria-label={`为 ${branch.format} 添加步骤`}
              onClick={() =>
                updateSteps(branch.format, [
                  ...branch.steps,
                  { adapter: '', required: true, conditions: [] },
                ])
              }
              className="rounded-tag border border-surface-border px-2 py-0.5 text-xs hover:bg-surface-muted"
            >
              添加步骤
            </button>
          </div>
        </div>
      ))}

      <div className="flex flex-wrap items-center gap-2 text-xs">
        <input
          aria-label="新格式名称"
          value={newFormat}
          onChange={(event) => setNewFormat(event.target.value)}
          placeholder="新格式，如 pdf"
          className="w-40 rounded-btn border border-surface-border bg-surface px-2 py-1"
        />
        <button
          type="button"
          disabled={!newFormat.trim() || duplicateFormat}
          onClick={addFormat}
          className="rounded-btn border border-surface-border px-3 py-1 hover:bg-surface-muted disabled:opacity-50"
        >
          添加格式
        </button>
        {duplicateFormat && <span className="text-state-warn">格式已存在</span>}
        {branches.length === 0 && (
          <span className="text-state-warn">尚未定义任何格式分支，保存前需补齐。</span>
        )}
      </div>
    </div>
  );
}
