import { useEffect } from 'react';
import { BrowserRouter, Navigate, Route, Routes, useNavigate } from 'react-router-dom';
import { request, setUnauthorizedHandler } from './api/client';
import { AppShell } from './components/AppShell';
import { ApplicationsSettingsPage } from './pages/ApplicationsSettingsPage';
import { ChatPage } from './pages/ChatPage';
import { DatasetsPage } from './pages/DatasetsPage';
import { DocumentsPage } from './pages/DocumentsPage';
import { LoginPage } from './pages/LoginPage';
import { ModelsSettingsPage } from './pages/ModelsSettingsPage';
import { RetrievalPage } from './pages/RetrievalPage';
import { PipelinesPage } from './pages/PipelinesPage';
import { QualityPage } from './pages/QualityPage';
import { ReviewPage } from './pages/ReviewPage';
import { StorageSettingsPage } from './pages/StorageSettingsPage';
import { TaskDetailPage } from './pages/TaskDetailPage';
import { TasksPage } from './pages/TasksPage';
import { WorkbenchPage } from './pages/WorkbenchPage';
import { useSessionStore } from './stores/session';

function RequireSession({ children }: { children: React.ReactNode }) {
  const user = useSessionStore((s) => s.user);
  if (!user) {
    return <Navigate to="/login" replace />;
  }
  return <>{children}</>;
}

/** 设置页仅租户 admin 可见；服务端仍独立鉴权（此处仅为交互提示）。 */
function RequireTenantAdmin({ children }: { children: React.ReactNode }) {
  const tenants = useSessionStore((s) => s.tenants);
  const currentTenantId = useSessionStore((s) => s.currentTenantId);
  const role = tenants.find((t) => t.tenant_id === currentTenantId)?.role;
  if (role !== 'admin') {
    return <Navigate to="/datasets" replace />;
  }
  return <>{children}</>;
}

function SessionBootstrap() {
  const setSession = useSessionStore((s) => s.setSession);
  const setTenants = useSessionStore((s) => s.setTenants);
  const navigate = useNavigate();

  useEffect(() => {
    // 401 统一清会话并回登录（不能仅靠路由守卫）
    setUnauthorizedHandler(() => {
      useSessionStore.getState().clear();
      navigate('/login', { replace: true });
    });
  }, [navigate]);

  useEffect(() => {
    let cancelled = false;
    async function bootstrap() {
      try {
        const me = await request<import('./api/client').UserMe & { csrf_token: string }>('/auth/me');
        const tenants = await request<import('./api/client').TenantEntry[]>('/auth/tenants');
        if (!cancelled) {
          // me.csrf_token 为刷新后重签发的 CSRF（服务端只存摘要）
          setSession(me, me.csrf_token, tenants);
          setTenants(tenants);
        }
      } catch {
        // 未登录：保持空会话，由路由守卫跳转
      }
    }
    void bootstrap();
    return () => {
      cancelled = true;
    };
  }, [setSession, setTenants]);

  return null;
}

export function App() {
  return (
    <BrowserRouter>
      <SessionBootstrap />
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          path="/*"
          element={
            <RequireSession>
              <AppShell />
            </RequireSession>
          }
        >
          <Route index element={<Navigate to="/datasets" replace />} />
          <Route path="datasets" element={<DatasetsPage />} />
          <Route path="chat" element={<ChatPage />} />
          <Route path="documents" element={<DocumentsPage />} />
          <Route path="tasks" element={<TasksPage />} />
          <Route path="retrieval" element={<RetrievalPage />} />
          <Route path="review" element={<ReviewPage />} />
          <Route path="quality" element={<QualityPage />} />
          <Route path="settings/pipelines" element={<PipelinesPage />} />
          <Route path="tasks/:jobId" element={<TaskDetailPage />} />
          <Route path="documents/:documentId" element={<WorkbenchPage />} />
          <Route
            path="settings/applications"
            element={
              <RequireTenantAdmin>
                <ApplicationsSettingsPage />
              </RequireTenantAdmin>
            }
          />
          <Route
            path="settings/storage"
            element={
              <RequireTenantAdmin>
                <StorageSettingsPage />
              </RequireTenantAdmin>
            }
          />
          <Route
            path="settings/models"
            element={
              <RequireTenantAdmin>
                <ModelsSettingsPage />
              </RequireTenantAdmin>
            }
          />
          <Route path="*" element={<Navigate to="/datasets" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
