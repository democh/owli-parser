"""retrieval：召回、过滤、重排与检索测试。

- 默认 dense + BM25（双通道基线）；learned sparse 与 multi-vector 是独立增强，
  不与 BM25 混称，启用需先证明收益。
- 所有通道前置过滤 tenant/dataset/ACL，返回内容前再校验权威版本与权限。
"""
