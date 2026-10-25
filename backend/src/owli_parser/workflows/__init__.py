"""workflows：跨模块提交、发布、删除等事务编排。

- 每个 workflow 共享一个 UoW；网络 IO 与模型推理在事务外。
- 模块不得反向依赖 workflows；workflows 只调用各模块公开 service。
"""
