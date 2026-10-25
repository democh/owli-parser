/**
 * TokenSecretDialog：签发后的原始 Token 一次性展示。
 * - 明文只存在于本组件内存；关闭即清空（由父组件卸载保证），不写 store/埋点/日志。
 * - 提供主动复制；“关闭后无法再次查看”提示与无障碍焦点处理。
 */
import { useEffect, useRef, useState } from 'react';
import type { IssuedToken } from '../api/client';

export function TokenSecretDialog({
  issued,
  onClose,
}: {
  issued: IssuedToken;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const [visible, setVisible] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const copyRef = useRef(issued.token);

  useEffect(() => {
    dialogRef.current?.focus();
    return () => {
      // 卸载即清除引用，避免明文被意外留存
      copyRef.current = '';
    };
  }, []);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(copyRef.current);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="presentation"
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="token-dialog-title"
        tabIndex={-1}
        className="w-full max-w-lg rounded-card border border-surface-border bg-surface p-6 shadow-overlay"
      >
        <h2 id="token-dialog-title" className="text-base font-semibold text-ink">
          Token 已签发
        </h2>
        <p role="alert" className="mt-2 text-sm text-state-warn">
          关闭本窗口后无法再次查看该 Token。请立即复制并妥善保存。
        </p>
        <div className="mt-4">
          <div className="text-xs text-ink-muted">Token（仅本次展示）</div>
          <div className="mt-1 flex items-center gap-2">
            <code
              className="min-w-0 flex-1 truncate rounded-btn border border-surface-border bg-surface-muted px-3 py-2 font-mono text-xs"
              data-testid="token-secret-value"
            >
              {visible ? copyRef.current : '•'.repeat(Math.min(copyRef.current.length, 48))}
            </code>
            <button
              type="button"
              onClick={() => setVisible((v) => !v)}
              className="rounded-btn border border-surface-border px-2 py-1.5 text-xs"
            >
              {visible ? '隐藏' : '显示'}
            </button>
            <button
              type="button"
              onClick={() => void handleCopy()}
              className="rounded-btn bg-brand px-3 py-1.5 text-xs text-white"
            >
              {copied ? '已复制' : '复制'}
            </button>
          </div>
        </div>
        <dl className="mt-4 grid grid-cols-2 gap-2 text-xs text-ink-muted">
          <dt>public_id</dt>
          <dd className="font-mono">{issued.public_id}</dd>
          <dt>到期时间</dt>
          <dd className="tabular-nums">
            {new Date(issued.expires_at_epoch * 1000).toLocaleString()}
          </dd>
          <dt>scopes</dt>
          <dd>{issued.scopes.join(', ')}</dd>
          <dt>数据集数量</dt>
          <dd className="tabular-nums">{issued.dataset_ids.length}</dd>
        </dl>
        <div className="mt-6 flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="rounded-btn bg-ink px-4 py-2 text-sm text-surface"
          >
            我已保存，关闭
          </button>
        </div>
      </div>
    </div>
  );
}
