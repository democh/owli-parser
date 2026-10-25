/**
 * 用途绑定编辑（租户 admin）：连接 → 档案 → 绑定；预算为自由 JSON（当前支持
 * generation.max_tokens_per_run）。首次创建免 If-Match，更新携带 revision。
 *
 * 纯展示组件：数据与保存回调由页面注入，便于单测覆盖未绑定/已绑定/保存中/错误态。
 */
import { useEffect, useState } from 'react';
import type { ModelBinding, ModelProfile } from '../api/client';
import { Card } from './shared';

const PURPOSES = [
  { value: 'generation', label: '生成（问答）', capability: 'generation' },
  { value: 'embedding', label: '向量（检索）', capability: 'embedding' },
  { value: 'rerank', label: '重排', capability: 'rerank' },
  { value: 'vision', label: '视觉', capability: 'vision' },
] as const;

export interface BindingSavePayload {
  purpose: string;
  primaryProfileId: string;
  expectedRevision: number | null;
  /** null = 不修改（非 generation）；{} = 显式清空预算。 */
  budget: Record<string, unknown> | null;
}

interface Draft {
  profileId: string;
  budget: string;
}

interface Props {
  profiles: ModelProfile[];
  bindings: ModelBinding[];
  savingPurpose: string | null;
  error: string | null;
  onSave: (payload: BindingSavePayload) => void;
}

export function ModelBindingsBoard({ profiles, bindings, savingPurpose, error, onSave }: Props) {
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});

  useEffect(() => {
    setDrafts((prev) => {
      const next = { ...prev };
      let changed = false;
      for (const purpose of PURPOSES) {
        const binding = bindings.find((item) => item.purpose === purpose.value);
        const available = profiles.filter(
          (profile) =>
            profile.state === 'published' && profile.capabilities.includes(purpose.capability),
        );
        const existing = next[purpose.value];
        const valid =
          existing !== undefined &&
          (available.some((profile) => profile.id === existing.profileId) ||
            binding?.primary_profile_id === existing.profileId);
        if (valid) continue;
        next[purpose.value] = {
          profileId:
            binding?.primary_profile_id ?? available[0]?.id ?? '',
          budget:
            existing?.budget ??
            String(
              typeof binding?.budget['max_tokens_per_run'] === 'number'
                ? binding.budget['max_tokens_per_run']
                : '',
            ),
        };
        changed = true;
      }
      return changed ? next : prev;
    });
  }, [profiles, bindings]);

  const save = (purpose: (typeof PURPOSES)[number]) => {
    const draft = drafts[purpose.value];
    if (!draft?.profileId) return;
    const binding = bindings.find((item) => item.purpose === purpose.value);
    let budget: Record<string, unknown> | null = null;
    if (purpose.value === 'generation') {
      const parsed = Number(draft.budget);
      budget =
        draft.budget.trim() !== '' && Number.isFinite(parsed) && parsed > 0
          ? { max_tokens_per_run: Math.floor(parsed) }
          : {};
    }
    onSave({
      purpose: purpose.value,
      primaryProfileId: draft.profileId,
      expectedRevision: binding?.revision ?? null,
      budget,
    });
  };

  return (
    <Card>
      <div className="border-b border-surface-border px-4 py-2 text-sm font-medium">
        用途绑定（检索 / 问答）
      </div>
      <div className="p-4 text-xs text-ink-muted">
        绑定只引用已发布档案；生成预算为空表示不限制，超出预算的问答会以 budget_exceeded 中止。
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-y border-surface-border text-left text-xs text-ink-muted">
              <th className="px-4 py-2 font-medium">用途</th>
              <th className="px-4 py-2 font-medium">当前绑定</th>
              <th className="px-4 py-2 font-medium">能力档案</th>
              <th className="px-4 py-2 font-medium">预算 max_tokens_per_run</th>
              <th className="px-4 py-2 font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {PURPOSES.map((purpose) => {
              const binding = bindings.find((item) => item.purpose === purpose.value);
              const available = profiles.filter(
                (profile) =>
                  profile.state === 'published' &&
                  profile.capabilities.includes(purpose.capability),
              );
              const draft = drafts[purpose.value] ?? { profileId: '', budget: '' };
              const saving = savingPurpose === purpose.value;
              return (
                <tr key={purpose.value} className="border-b border-surface-border last:border-0">
                  <td className="px-4 py-3">
                    <div>{purpose.label}</div>
                    <div className="font-mono text-xs text-ink-muted">{purpose.value}</div>
                  </td>
                  <td className="px-4 py-3 text-xs">
                    {binding ? (
                      <div data-testid={`binding-${purpose.value}`}>
                        <div>
                          {binding.primary_model_name ?? binding.primary_profile_id.slice(0, 8)} · r
                          {binding.revision}
                        </div>
                        <div className="text-ink-muted">
                          预算：
                          {typeof binding.budget['max_tokens_per_run'] === 'number'
                            ? `${binding.budget['max_tokens_per_run']} tokens`
                            : '不限'}
                        </div>
                      </div>
                    ) : (
                      <span className="text-ink-muted">未绑定</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <select
                      aria-label={`${purpose.label} 档案`}
                      value={draft.profileId}
                      onChange={(event) =>
                        setDrafts((prev) => ({
                          ...prev,
                          [purpose.value]: { ...draft, profileId: event.target.value },
                        }))
                      }
                      className="w-full min-w-[12rem] rounded-btn border border-surface-border bg-surface px-2 py-1 text-xs"
                    >
                      <option value="">选择已发布档案</option>
                      {available.map((profile) => (
                        <option key={profile.id} value={profile.id}>
                          {profile.model_name}（{profile.id.slice(0, 8)}）
                        </option>
                      ))}
                      {draft.profileId && !available.some((p) => p.id === draft.profileId) && (
                        <option value={draft.profileId}>当前档案（不可用）</option>
                      )}
                    </select>
                    {available.length === 0 && (
                      <p className="mt-1 text-xs text-ink-muted">
                        暂无具备 {purpose.capability} 能力的已发布档案
                      </p>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    {purpose.value === 'generation' ? (
                      <input
                        aria-label="生成预算 tokens"
                        type="number"
                        min={1}
                        placeholder="不限"
                        value={draft.budget}
                        onChange={(event) =>
                          setDrafts((prev) => ({
                            ...prev,
                            [purpose.value]: { ...draft, budget: event.target.value },
                          }))
                        }
                        className="w-28 rounded-btn border border-surface-border bg-surface px-2 py-1 text-xs"
                      />
                    ) : (
                      <span className="text-xs text-ink-muted">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <button
                      type="button"
                      aria-label={`保存${purpose.label}绑定`}
                      disabled={saving || !draft.profileId}
                      onClick={() => save(purpose)}
                      className="rounded-btn border border-surface-border px-3 py-1 text-xs hover:bg-surface-muted disabled:opacity-50"
                    >
                      {saving ? '保存中…' : '保存绑定'}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {error && (
        <p role="alert" data-testid="binding-error" className="px-4 pb-3 text-xs text-state-danger">
          {error}
        </p>
      )}
    </Card>
  );
}
