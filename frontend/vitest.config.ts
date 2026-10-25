import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// 测试配置与构建配置分离，避免 Vite 版本类型交叉污染。
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    globals: true,
  },
});
