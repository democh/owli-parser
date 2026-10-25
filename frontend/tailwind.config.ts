import type { Config } from 'tailwindcss';

// 设计 token 集中维护（文档前端 §11.2）：颜色/圆角/阴影/字体从这里出发。
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'media',
  theme: {
    extend: {
      colors: {
        surface: {
          DEFAULT: 'rgb(var(--owli-surface) / <alpha-value>)',
          muted: 'rgb(var(--owli-surface-muted) / <alpha-value>)',
          border: 'rgb(var(--owli-border) / <alpha-value>)',
        },
        ink: {
          DEFAULT: 'rgb(var(--owli-ink) / <alpha-value>)',
          muted: 'rgb(var(--owli-ink-muted) / <alpha-value>)',
        },
        brand: {
          DEFAULT: 'rgb(var(--owli-brand) / <alpha-value>)',
          soft: 'rgb(var(--owli-brand-soft) / <alpha-value>)',
        },
        state: {
          ok: 'rgb(var(--owli-ok) / <alpha-value>)',
          warn: 'rgb(var(--owli-warn) / <alpha-value>)',
          danger: 'rgb(var(--owli-danger) / <alpha-value>)',
        },
      },
      borderRadius: {
        card: '12px',
        btn: '8px',
        tag: '6px',
      },
      boxShadow: {
        rest: '0 1px 2px rgb(0 0 0 / 0.06)',
        hover: '0 2px 8px rgb(0 0 0 / 0.10)',
        overlay: '0 8px 32px rgb(0 0 0 / 0.18)',
      },
      fontFamily: {
        sans: ['Inter', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'sans-serif'],
      },
    },
  },
  plugins: [],
} satisfies Config;
