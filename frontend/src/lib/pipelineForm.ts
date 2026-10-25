/**
 * 管道 YAML ↔ 表单模型（M5-1 表单模式）。
 *
 * - 只重建 `pipelines` 段；schema_version/name/revision 等顶层字段原样保留；
 * - step 的 `when` 条件原样保留（表单不编辑条件，但不丢数据）；
 * - 解析失败返回 error 而不抛异常（表单降级为提示，用户可切回 YAML）。
 */
import { parse, stringify } from 'yaml';

export type PipelineMode = 'first_success' | 'sequence';

/** 后端 when 运算符白名单（modules/parsing/pipeline.py）。 */
export const WHEN_OPERATORS = ['gte', 'lte', 'gt', 'lt', 'eq', 'ne', 'in'] as const;
export type WhenOperator = (typeof WHEN_OPERATORS)[number];

export interface WhenConditionForm {
  feature: string;
  operator: WhenOperator;
  /** 值以 JSON 文本编辑；裸文本按字符串处理。 */
  valueText: string;
}

export interface PipelineFormStep {
  adapter: string;
  required: boolean;
  conditions: WhenConditionForm[];
}

export interface PipelineFormBranch {
  format: string;
  mode: PipelineMode;
  steps: PipelineFormStep[];
}

export type PipelineFormParse =
  | { ok: true; doc: Record<string, unknown>; branches: PipelineFormBranch[] }
  | { ok: false; error: string };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function readWhen(
  when: unknown,
): { ok: true; conditions: WhenConditionForm[] } | { ok: false; error: string } {
  if (when === undefined || when === null) return { ok: true, conditions: [] };
  if (!isRecord(when)) return { ok: false, error: 'when 必须是对象' };
  const conditions: WhenConditionForm[] = [];
  for (const [feature, spec] of Object.entries(when)) {
    if (!isRecord(spec)) return { ok: false, error: `when.${feature} 必须是运算符对象` };
    const entries = Object.entries(spec);
    if (entries.length !== 1) {
      return { ok: false, error: `when.${feature} 必须且只能包含一个运算符` };
    }
    const [operator, value] = entries[0]!;
    if (!WHEN_OPERATORS.includes(operator as WhenOperator)) {
      return { ok: false, error: `when.${feature} 运算符 ${operator} 不在白名单` };
    }
    conditions.push({
      feature,
      operator: operator as WhenOperator,
      valueText: JSON.stringify(value),
    });
  }
  return { ok: true, conditions };
}

function parseWhenValue(text: string): unknown {
  const trimmed = text.trim();
  if (trimmed === '') return '';
  try {
    return JSON.parse(trimmed);
  } catch {
    return text;
  }
}

function buildWhen(
  conditions: WhenConditionForm[],
): Record<string, unknown> | undefined {
  const when: Record<string, unknown> = {};
  for (const condition of conditions) {
    const feature = condition.feature.trim();
    if (!feature) continue; // 未填特征的条件视为未完成，不写入 YAML
    when[feature] = { [condition.operator]: parseWhenValue(condition.valueText) };
  }
  return Object.keys(when).length > 0 ? when : undefined;
}

/** YAML 文本 → 表单模型（分支按格式名排序，缺省 required=true）。 */
export function parsePipelineForm(yamlText: string): PipelineFormParse {
  let parsed: unknown;
  try {
    parsed = parse(yamlText);
  } catch (error) {
    const detail = error instanceof Error ? error.message : '未知错误';
    return { ok: false, error: `YAML 解析失败：${detail}` };
  }
  if (!isRecord(parsed)) {
    return { ok: false, error: 'YAML 顶层必须是对象' };
  }
  const pipelines = parsed.pipelines;
  if (!isRecord(pipelines)) {
    return { ok: false, error: '缺少 pipelines 段（对象）' };
  }
  const branches: PipelineFormBranch[] = [];
  for (const [format, raw] of Object.entries(pipelines)) {
    // 畸形结构直接报错：表单会整体回写 YAML，跳过等于静默删数据
    if (!isRecord(raw)) {
      return { ok: false, error: `pipelines.${format} 必须是对象` };
    }
    if (raw.mode !== 'first_success' && raw.mode !== 'sequence') {
      return { ok: false, error: `pipelines.${format}.mode 仅支持 first_success/sequence` };
    }
    const mode: PipelineMode = raw.mode;
    const list = mode === 'sequence' ? raw.steps : raw.candidates;
    const steps: PipelineFormStep[] = [];
    if (list !== undefined && !Array.isArray(list)) {
      const key = mode === 'sequence' ? 'steps' : 'candidates';
      return { ok: false, error: `pipelines.${format}.${key} 必须是数组` };
    }
    for (const [index, item] of (list ?? []).entries()) {
      if (!isRecord(item)) {
        return { ok: false, error: `pipelines.${format} 的步骤必须是对象` };
      }
      const conditions = readWhen(item.when);
      if (!conditions.ok) {
        return { ok: false, error: `pipelines.${format} 的步骤 ${index + 1} ${conditions.error}` };
      }
      steps.push({
        adapter: typeof item.adapter === 'string' ? item.adapter : '',
        required: item.required !== false,
        conditions: conditions.conditions,
      });
    }
    branches.push({ format, mode, steps });
  }
  branches.sort((a, b) => a.format.localeCompare(b.format));
  return { ok: true, doc: parsed, branches };
}

/** 表单模型 → YAML 文本（注释与原始排版会被规范化，由 UI 明确提示）。 */
export function serializePipelineForm(
  doc: Record<string, unknown>,
  branches: PipelineFormBranch[],
): string {
  const pipelines: Record<string, unknown> = {};
  for (const branch of branches) {
    const steps = branch.steps.map((step) => {
      const when = buildWhen(step.conditions);
      return {
        adapter: step.adapter,
        required: step.required,
        ...(when ? { when } : {}),
      };
    });
    pipelines[branch.format] =
      branch.mode === 'sequence'
        ? { mode: 'sequence', steps }
        : { mode: 'first_success', candidates: steps };
  }
  return stringify({ ...doc, pipelines }, { lineWidth: 0 });
}
