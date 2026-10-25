import '@testing-library/jest-dom/vitest';

// React Flow 依赖 ResizeObserver；jsdom 未实现，提供空实现即可满足挂载
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

// jsdom 未实现 canvas 2D；返回 null 与浏览器“无法取上下文”的降级语义一致，
// 同时避免每条 PDF 预览用例向 stderr 打未实现堆栈。
if (typeof HTMLCanvasElement !== 'undefined') {
  Object.defineProperty(HTMLCanvasElement.prototype, 'getContext', {
    configurable: true,
    value: () => null,
  });
}
