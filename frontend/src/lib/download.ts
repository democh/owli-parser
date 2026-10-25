/**
 * 前端导出工具：受权页面内生成文本文件并触发下载（无后端参与）。
 *
 * - 导出内容仅限当前用户已能查看的数据（diff 摘要/指标），不含额外权限面；
 * - jsdom/受限环境不支持 createObjectURL 时返回 false（调用方可提示失败，
 *   不静默）。
 */

export function downloadText(filename: string, text: string, mime = 'text/plain;charset=utf-8'): boolean {
  try {
    if (typeof URL.createObjectURL !== 'function') {
      return false;
    }
    const blob = new Blob([text], { type: mime });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    URL.revokeObjectURL(url);
    return true;
  } catch {
    return false;
  }
}

/** CSV 字段转义：含逗号/引号/换行时加引号并转义内部引号。 */
export function csvField(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') {
    return '';
  }
  const text = String(value);
  if (/[",\n]/.test(text)) {
    return `"${text.replace(/"/g, '""')}"`;
  }
  return text;
}

/** 时间戳文件名（本地时区，形如 2026-10-15T2230）。 */
export function timestampedName(prefix: string, extension: string): string {
  const now = new Date();
  const pad = (value: number) => String(value).padStart(2, '0');
  const stamp = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}${pad(now.getMinutes())}`;
  return `${prefix}-${stamp}.${extension}`;
}
