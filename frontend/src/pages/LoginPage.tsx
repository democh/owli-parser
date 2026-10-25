import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Navigate, useNavigate } from 'react-router-dom';
import { ApiError, request, type LoginResponse, type TenantEntry } from '../api/client';
import { useSessionStore } from '../stores/session';

interface LoginForm {
  username: string;
  password: string;
}

export function LoginPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const setSession = useSessionStore((s) => s.setSession);
  const user = useSessionStore((s) => s.user);

  const login = useMutation({
    mutationFn: async (form: LoginForm) => {
      const response = await request<LoginResponse>('/auth/login', {
        method: 'POST',
        body: form,
      });
      const tenants = await request<TenantEntry[]>('/auth/tenants');
      return { response, tenants };
    },
    onSuccess: ({ response, tenants }) => {
      queryClient.clear();
      setSession(response, response.csrf_token, tenants);
      navigate('/datasets', { replace: true });
    },
  });

  if (user) {
    // 已恢复会话时直接回到工作台，不重复登录
    return <Navigate to="/datasets" replace />;
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-surface-muted p-4">
      <form
        className="w-full max-w-sm rounded-card border border-surface-border bg-surface p-8 shadow-overlay"
        onSubmit={(event) => {
          event.preventDefault();
          const data = new FormData(event.currentTarget);
          login.mutate({
            username: String(data.get('username') ?? ''),
            password: String(data.get('password') ?? ''),
          });
        }}
      >
        <h1 className="text-lg font-semibold text-ink">owli-parser</h1>
        <p className="mt-1 text-xs text-ink-muted">企业知识库文件解析与工作台</p>
        <label className="mt-6 block text-sm">
          <span className="text-ink-muted">用户名</span>
          <input
            name="username"
            autoComplete="username"
            required
            className="mt-1 w-full rounded-btn border border-surface-border bg-surface px-3 py-2 text-sm"
          />
        </label>
        <label className="mt-4 block text-sm">
          <span className="text-ink-muted">密码</span>
          <input
            name="password"
            type="password"
            autoComplete="current-password"
            required
            className="mt-1 w-full rounded-btn border border-surface-border bg-surface px-3 py-2 text-sm"
          />
        </label>
        {login.isError && (
          <p role="alert" className="mt-4 text-sm text-state-danger">
            {login.error instanceof ApiError
              ? login.error.code === 'rate_limited'
                ? '登录尝试过于频繁，请稍后再试'
                : login.error.message
              : '登录失败，请重试'}
          </p>
        )}
        <button
          type="submit"
          disabled={login.isPending}
          className="mt-6 w-full rounded-btn bg-brand px-3 py-2 text-sm font-medium text-white disabled:opacity-60"
        >
          {login.isPending ? '登录中…' : '登录'}
        </button>
      </form>
    </div>
  );
}
