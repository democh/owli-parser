"""业务模块集合。

模块清单：identity、access、catalog、artifacts、ingestion、jobs、parsing、
knowledge、retrieval、chat、configuration。模块只依赖 core/contracts/ports
与自身；跨模块协作走公开 service/DTO 或 workflows。
"""
