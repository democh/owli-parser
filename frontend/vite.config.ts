import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// 开发模式反代到后端；生产构建由同源反向代理托管。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/v1': {
        target: process.env.OWLI_API_PROXY ?? 'http://127.0.0.1:8000',
        changeOrigin: true,
        ws: true, // 任务事件 WebSocket 经开发服务器代理
      },
      '/healthz': {
        target: process.env.OWLI_API_PROXY ?? 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
});
