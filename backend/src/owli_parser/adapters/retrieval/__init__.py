"""检索索引适配器：内存实现用于开发/测试与 contract suite。

M3 将接入 Qdrant（dense）与 OpenSearch（BM25）适配器，行为以同一
contract suite 验收；此处内存实现保证端口语义可离线验证。
"""
