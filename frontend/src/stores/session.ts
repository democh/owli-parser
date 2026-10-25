/**
 * 会话与租户选择（客户端轻量状态，不存服务端整库）。
 * - CSRF 只存内存；切换租户清理选择；敏感值不持久化。
 */
import { create } from 'zustand';
import type { TenantEntry, UserMe } from '../api/client';

interface SessionState {
  user: UserMe | null;
  csrfToken: string | null;
  tenants: TenantEntry[];
  currentTenantId: string | null;
  /** 会话恢复是否已完成：未完成前不判定为未登录（避免刷新即跳登录页）。 */
  initialized: boolean;
  setSession: (user: UserMe, csrfToken: string, tenants: TenantEntry[]) => void;
  setTenants: (tenants: TenantEntry[]) => void;
  selectTenant: (tenantId: string) => void;
  markInitialized: () => void;
  clear: () => void;
}

export const useSessionStore = create<SessionState>((set) => ({
  user: null,
  csrfToken: null,
  tenants: [],
  currentTenantId: null,
  initialized: false,
  setSession: (user, csrfToken, tenants) =>
    set({
      user,
      csrfToken,
      tenants,
      currentTenantId: pickInitialTenant(tenants, null),
      initialized: true,
    }),
  setTenants: (tenants) =>
    set((state) => ({
      tenants,
      currentTenantId: pickInitialTenant(tenants, state.currentTenantId),
    })),
  selectTenant: (tenantId) => set({ currentTenantId: tenantId }),
  markInitialized: () => set({ initialized: true }),
  // 登出/401：关闭一切租户上下文（正文缓存由 React Query 清理）
  clear: () =>
    set({ user: null, csrfToken: null, tenants: [], currentTenantId: null, initialized: true }),
}));

function pickInitialTenant(tenants: TenantEntry[], current: string | null): string | null {
  if (current && tenants.some((t) => t.tenant_id === current)) {
    return current;
  }
  return tenants[0]?.tenant_id ?? null;
}
