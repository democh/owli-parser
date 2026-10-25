"""检索索引适配器。

- Qdrant：dense（REST /points/query + payload 前置过滤）；
- OpenSearch：BM25 词法（_bulk + bool.filter）；
- in_memory：开发/测试与端口 contract 基线。

三者共享 tenant/dataset/ACL/generation 前置过滤、幂等写入与旧代清理语义。
"""
