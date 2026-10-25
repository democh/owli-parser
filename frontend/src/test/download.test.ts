/** 导出工具测试：CSV 转义、时间戳文件名、下载触发与受限环境回退。 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { csvField, downloadText, timestampedName } from '../lib/download';

afterEach(() => {
  vi.restoreAllMocks();
});

describe('csvField', () => {
  it('普通值原样输出；空值输出空串', () => {
    expect(csvField('abc')).toBe('abc');
    expect(csvField(3)).toBe('3');
    expect(csvField(null)).toBe('');
    expect(csvField(undefined)).toBe('');
    expect(csvField('')).toBe('');
  });

  it('含逗号/引号/换行的值加引号并转义内部引号', () => {
    expect(csvField('a,b')).toBe('"a,b"');
    expect(csvField('say "hi"')).toBe('"say ""hi"""');
    expect(csvField('line1\nline2')).toBe('"line1\nline2"');
  });
});

describe('timestampedName', () => {
  it('生成前缀-时间戳.扩展名 且分钟零填充', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-15T09:05:00'));
    const name = timestampedName('retrieval-diff', 'csv');
    vi.useRealTimers();
    expect(name).toBe('retrieval-diff-2026-10-15T0905.csv');
  });
});

describe('downloadText', () => {
  it('支持 createObjectURL 时触发下载并返回 true', () => {
    const createObjectURL = vi.fn(() => 'blob:fake');
    const revokeObjectURL = vi.fn();
    vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL });
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);

    expect(downloadText('x.csv', 'a,b')).toBe(true);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:fake');
    expect(clickSpy).toHaveBeenCalledTimes(1);
  });

  it('受限环境（无 createObjectURL）返回 false，不静默', () => {
    vi.stubGlobal('URL', {});
    expect(downloadText('x.csv', 'a,b')).toBe(false);
  });
});
