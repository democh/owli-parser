/**
 * Adapter 注册表管理（M4-6，平台管理员）：查看内置/外部实例，注册或上报心跳。
 *
 * - 写操作由后端强制平台管理员；本组件只在 `visible` 时渲染，避免普通成员误触；
 * - capabilities 以 JSON 文本编辑，提交前解析并校验为对象（不把坏 JSON 发给服务端）；
 * - 心跳仅对已注册的外部实例展示；内置实例由进程内注册表管理。
 */
import { useState } from 'react';
import type { AdapterInfo, AdapterRegisterInput } from '../api/client';

const MODES = ['remote', 'local', 'composite'] as const;
const HEALTHS = ['ready', 'degraded', 'maintenance', 'unavailable'] as const;

export interface AdapterRegistryPanelProps {
  items: AdapterInfo[];
  /** 仅平台管理员展示（会话中的 is_platform_admin）。 */
  visible: boolean;
  registering?: boolean;
  heartbeatName?: string | null;
  error?: string | null;
  onRegister: (values: AdapterRegisterInput) => void;
  onHeartbeat: (name: string) => void;
}

function formatHeartbeat(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function healthClass(health: string): string {
  if (health === 'ready') return 'text-state-ok';
  if (health === 'unavailable') return 'text-state-danger';
  return 'text-state-warn';
}

export function AdapterRegistryPanel({
  items,
  visible,
  registering = false,
  heartbeatName = null,
  error = null,
  onRegister,
  onHeartbeat,
}: AdapterRegistryPanelProps) {
  const [name, setName] = useState('');
  const [version, setVersion] = useState('1.0.0');
  const [mode, setMode] = useState<string>('remote');
  const [health, setHealth] = useState<string>('ready');
  const [configRef, setConfigRef] = useState('');
  const [capabilitiesText, setCapabilitiesText] = useState('{}');
  const [formError, setFormError] = useState<string | null>(null);

  if (!visible) return null;

  const submit = () => {
    const clean = name.trim();
    if (!clean) {
      setFormError('adapter 名称必填');
      return;
    }
    if (!version.trim()) {
      setFormError('版本必填');
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(capabilitiesText.trim() || '{}');
    } catch {
      setFormError('能力必须是合法 JSON');
      return;
    }
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      setFormError('能力必须是 JSON 对象');
      return;
    }
    setFormError(null);
    onRegister({
      name: clean,
      version: version.trim(),
      mode,
      health,
      configRef: configRef.trim() || null,
      capabilities: parsed as Record<string, unknown>,
    });
  };

  return (
    <details
      data-testid="adapter-registry-panel"
      className="rounded-card border border-surface-border p-3 text-xs"
    >
      <summary className="cursor-pointer text-ink-muted">
        Adapter 注册表（平台管理员：外部实例注册/心跳）
      </summary>

      {error && (
        <p role="alert" className="mt-2 text-state-danger">
          {error}
        </p>
      )}

      <div className="mt-2 overflow-x-auto">
        <table data-testid="adapter-registry-table" className="w-full text-xs">
          <thead>
            <tr className="border-b border-surface-border text-left text-ink-muted">
              <th className="px-2 py-1">名称</th>
              <th className="px-2 py-1">来源</th>
              <th className="px-2 py-1">版本</th>
              <th className="px-2 py-1">模式</th>
              <th className="px-2 py-1">健康</th>
              <th className="px-2 py-1">最近心跳</th>
              <th className="px-2 py-1">操作</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.name} className="border-b border-surface-border last:border-b-0">
                <td className="px-2 py-1 font-mono">{item.name}</td>
                <td className="px-2 py-1">{item.source === 'registered' ? '外部' : '内置'}</td>
                <td className="px-2 py-1 tabular-nums">{item.version}</td>
                <td className="px-2 py-1">{item.mode}</td>
                <td className={`px-2 py-1 ${healthClass(item.health)}`}>
                  {item.health}
                  {item.stale ? '（失联）' : ''}
                </td>
                <td className="px-2 py-1 tabular-nums">{formatHeartbeat(item.heartbeat_at)}</td>
                <td className="px-2 py-1">
                  {item.source === 'registered' ? (
                    <button
                      type="button"
                      aria-label={`发送心跳 ${item.name}`}
                      disabled={heartbeatName === item.name}
                      onClick={() => onHeartbeat(item.name)}
                      className="rounded-tag border border-surface-border px-2 py-0.5 hover:bg-surface-muted disabled:opacity-50"
                    >
                      {heartbeatName === item.name ? '心跳中…' : '发送心跳'}
                    </button>
                  ) : (
                    '—'
                  )}
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={7} className="px-2 py-2 text-ink-muted">
                  暂无 adapter 数据
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="mt-3 space-y-2 border-t border-surface-border pt-2">
        <div className="text-ink-muted">注册 / 更新外部实例</div>
        <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
          <label className="space-y-1">
            <span className="block text-ink-muted">名称</span>
            <input
              aria-label="adapter 名称"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="compliance-ocr"
              className="w-full rounded-btn border border-surface-border bg-surface px-2 py-1 font-mono"
            />
          </label>
          <label className="space-y-1">
            <span className="block text-ink-muted">版本</span>
            <input
              aria-label="adapter 版本"
              value={version}
              onChange={(event) => setVersion(event.target.value)}
              className="w-full rounded-btn border border-surface-border bg-surface px-2 py-1 font-mono"
            />
          </label>
          <label className="space-y-1">
            <span className="block text-ink-muted">config 引用</span>
            <input
              aria-label="config 引用"
              value={configRef}
              onChange={(event) => setConfigRef(event.target.value)}
              placeholder="vault:adapters/name（可空）"
              className="w-full rounded-btn border border-surface-border bg-surface px-2 py-1 font-mono"
            />
          </label>
          <label className="space-y-1">
            <span className="block text-ink-muted">模式</span>
            <select
              aria-label="adapter 模式"
              value={mode}
              onChange={(event) => setMode(event.target.value)}
              className="w-full rounded-btn border border-surface-border bg-surface px-2 py-1"
            >
              {MODES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <label className="space-y-1">
            <span className="block text-ink-muted">健康度</span>
            <select
              aria-label="adapter 健康度"
              value={health}
              onChange={(event) => setHealth(event.target.value)}
              className="w-full rounded-btn border border-surface-border bg-surface px-2 py-1"
            >
              {HEALTHS.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
        </div>
        <label className="block space-y-1">
          <span className="block text-ink-muted">能力（JSON 对象）</span>
          <textarea
            aria-label="adapter 能力"
            rows={3}
            value={capabilitiesText}
            onChange={(event) => setCapabilitiesText(event.target.value)}
            spellCheck={false}
            className="w-full rounded-card border border-surface-border bg-surface p-2 font-mono"
          />
        </label>
        {formError && (
          <p role="alert" data-testid="adapter-register-error" className="text-state-danger">
            {formError}
          </p>
        )}
        <button
          type="button"
          disabled={registering}
          onClick={submit}
          className="rounded-btn bg-brand px-3 py-1 text-xs text-white disabled:opacity-50"
        >
          {registering ? '提交中…' : '注册 / 更新'}
        </button>
      </div>
    </details>
  );
}
