/**
 * 契约漂移检测：扫描前端所有 request('/...') 调用路径，与已提交的
 * contracts/openapi.json 快照逐段比对——后端删除/改路径时前端测试即失败。
 *
 * 约定：request() 的一级参数是去掉 /v1 前缀的路径模板；
 * `${...}` 参数、query string 在比对前归一化。
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const OPENAPI_PATH = join(__dirname, '../../../contracts/openapi.json');
const SRC_DIR = join(__dirname, '..');

interface OpenApiDoc {
  paths: Record<string, Record<string, unknown>>;
}

/** 归一化路径模板：
 * 1) 展开 ${...}（含被正则截断的残段，残段属 query 语义直接丢弃）；
 * 2) 段尾拼接参数 `${params}`（未以 / 分隔，惯用于 query 前缀）→ 剥离；
 * 3) 剥离 query、收尾斜杠。 */
export function normalizePath(raw: string): string {
  let path = raw;
  path = path.replace(/\$\{[^}]*\}/g, '{}');
  path = path.replace(/\$\{.*$/, ''); // 截断残段（如 `${cond ? ` 的 condition 部分）
  path = path.replace(/([^/{])\{\}$/, '$1'); // 段尾拼接参数（如 preview${params}）
  path = path.split('?')[0] ?? path;
  path = path.replace(/\/+$/, '');
  return path;
}

/** 段级匹配：openapi {param} 匹配任意模板段；模板 {} 必须是 openapi 参数段。 */
export function matchTemplate(template: string, openapiPath: string): boolean {
  const templateSegments = template.split('/').filter(Boolean);
  const specSegments = openapiPath.split('/').filter(Boolean);
  if (templateSegments.length !== specSegments.length) return false;
  return templateSegments.every((segment, index) => {
    const spec = specSegments[index] ?? '';
    const specIsParam = spec.startsWith('{') && spec.endsWith('}');
    if (segment === '{}') return specIsParam;
    if (specIsParam) return true;
    return segment === spec;
  });
}

function collectSourceFiles(dir: string): string[] {
  const entries: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    const stat = statSync(full);
    if (stat.isDirectory()) {
      if (name === 'test') continue;
      entries.push(...collectSourceFiles(full));
    } else if (/\.(ts|tsx)$/.test(name)) {
      entries.push(full);
    }
  }
  return entries;
}

const REQUEST_CALL = /request(?:<[^>]*>)?\(\s*([`'"])(\/[^`'"\n]*)/g;

function scanRequestPaths(): Array<{ call: string; file: string }> {
  const calls: Array<{ call: string; file: string }> = [];
  for (const file of collectSourceFiles(SRC_DIR)) {
    const content = readFileSync(file, 'utf-8');
    for (const match of content.matchAll(REQUEST_CALL)) {
      calls.push({ call: normalizePath(match[2] ?? ''), file: file.replace(SRC_DIR, 'src') });
    }
  }
  return calls;
}

describe('契约漂移检测', () => {
  const openapi = JSON.parse(readFileSync(OPENAPI_PATH, 'utf-8')) as OpenApiDoc;
  const specPaths = Object.keys(openapi.paths);

  it('openapi 快照可加载且包含关键路径', () => {
    expect(specPaths.length).toBeGreaterThan(50);
    expect(specPaths).toContain('/v1/retrieval/test');
    expect(specPaths).toContain('/v1/quality/regression-runs');
    expect(specPaths).toContain('/v1/auth/login');
  });

  it('normalizePath 归一化模板参数与 query', () => {
    expect(normalizePath('/documents/${documentId}/chunks?limit=200')).toBe(
      '/documents/{}/chunks',
    );
    expect(normalizePath('/datasets?limit=100')).toBe('/datasets');
    // 段尾 query 拼接参数（如 preview${params}）
    expect(normalizePath('/documents/${documentId}/preview${params}')).toBe(
      '/documents/{}/preview',
    );
    // 完整段参数（以 / 分隔）必须保留
    expect(normalizePath('/documents/${documentId}')).toBe('/documents/{}');
    // 嵌套模板串被正则截断的残段
    expect(normalizePath('/documents/${documentId}/blocks?limit=100${blockType ? `')).toBe(
      '/documents/{}/blocks',
    );
  });

  it('matchTemplate 段级匹配规则', () => {
    expect(matchTemplate('/documents/{}/chunks', '/v1/documents/{document_id}/chunks')).toBe(false);
    expect(matchTemplate('/v1/documents/{}/chunks', '/v1/documents/{document_id}/chunks')).toBe(
      true,
    );
    expect(matchTemplate('/v1/datasets', '/v1/datasets')).toBe(true);
    expect(matchTemplate('/v1/datasets', '/v1/datasets/{dataset_id}')).toBe(false);
    expect(matchTemplate('/v1/datasets/{}/documents', '/v1/datasets/{dataset_id}/documents')).toBe(
      true,
    );
  });

  it('全部前端 request 路径均存在于 OpenAPI 快照', () => {
    const calls = scanRequestPaths();
    expect(calls.length).toBeGreaterThan(30); // 扫描面保护：数量骤降说明正则失效
    const unmatched = calls.filter(({ call }) => {
      const full = `/v1${call}`;
      return !specPaths.some((specPath) => matchTemplate(full, specPath));
    });
    expect(unmatched, `未匹配的调用：${JSON.stringify(unmatched, null, 2)}`).toEqual([]);
  });
});
