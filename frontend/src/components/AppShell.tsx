/**
 * AppShell：租户切换 + 侧栏 + 内容区。
 * - 切换租户只改选择；旧租户请求由 React Query key（含 tenantId）自然隔离。
 */
import { NavLink, Outlet, useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { request } from '../api/client';
import { useSessionStore } from '../stores/session';

const NAV_ITEMS = [
  { to: '/datasets', label: '知识库' },
  { to: '/chat', label: '知识问答' },
  { to: '/documents', label: '文档中心' },
  { to: '/tasks', label: '任务中心' },
  { to: '/review', label: '复核队列' },
  { to: '/retrieval', label: '召回测试' },
  { to: '/quality', label: '质量回归' },
];

const ADMIN_NAV_ITEMS = [
  { to: '/settings/storage', label: '存储设置' },
  { to: '/settings/models', label: '模型设置' },
  { to: '/settings/applications', label: '应用与 API' },
  { to: '/settings/pipelines', label: '解析管道' },
];

export function AppShell() {
  const user = useSessionStore((s) => s.user);
  const tenants = useSessionStore((s) => s.tenants);
  const currentTenantId = useSessionStore((s) => s.currentTenantId);
  const selectTenant = useSessionStore((s) => s.selectTenant);
  const csrfToken = useSessionStore((s) => s.csrfToken);
  const clear = useSessionStore((s) => s.clear);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  async function handleLogout() {
    try {
      await request<void>('/auth/logout', { method: 'POST', csrfToken });
    } finally {
      clear();
      queryClient.clear(); // 清空全部租户正文缓存
      navigate('/login', { replace: true });
    }
  }

  const currentTenant = tenants.find((t) => t.tenant_id === currentTenantId);
  const isTenantAdmin = currentTenant?.role === 'admin';

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-60 flex-col border-r border-surface-border bg-surface">
        <div className="border-b border-surface-border px-4 py-4">
          <div className="text-sm font-semibold text-ink">owli-parser</div>
          <div className="mt-1 text-xs text-ink-muted">知识库工作台</div>
        </div>
        <div className="border-b border-surface-border px-4 py-3">
          <label htmlFor="tenant-select" className="mb-1 block text-xs text-ink-muted">
            当前租户
          </label>
          <select
            id="tenant-select"
            className="w-full rounded-tag border border-surface-border bg-surface px-2 py-1.5 text-sm"
            value={currentTenantId ?? ''}
            onChange={(e) => selectTenant(e.target.value)}
          >
            {tenants.map((t) => (
              <option key={t.tenant_id} value={t.tenant_id}>
                {t.name}（{t.role}）
              </option>
            ))}
          </select>
          {currentTenant?.tenant_status !== 'active' && (
            <p role="alert" className="mt-1 text-xs text-state-danger">
              该租户已停用，仅可查看历史
            </p>
          )}
        </div>
        <nav className="flex-1 px-2 py-3">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `block rounded-btn px-3 py-2 text-sm ${
                  isActive ? 'bg-brand-soft text-brand' : 'text-ink hover:bg-surface-muted'
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
          {isTenantAdmin && (
            <>
              <div className="px-3 pb-1 pt-4 text-xs text-ink-muted">设置</div>
              {ADMIN_NAV_ITEMS.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    `block rounded-btn px-3 py-2 text-sm ${
                      isActive ? 'bg-brand-soft text-brand' : 'text-ink hover:bg-surface-muted'
                    }`
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </>
          )}
        </nav>
        <div className="border-t border-surface-border px-4 py-3">
          <div className="text-sm text-ink">{user?.display_name}</div>
          <div className="text-xs text-ink-muted">{user?.username}</div>
          <button
            type="button"
            onClick={() => void handleLogout()}
            className="mt-2 w-full rounded-btn border border-surface-border px-2 py-1.5 text-xs text-ink hover:bg-surface-muted"
          >
            退出登录
          </button>
        </div>
      </aside>
      <main className="flex-1 overflow-x-auto p-6">
        <Outlet />
      </main>
    </div>
  );
}
