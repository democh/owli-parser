"""聚合全部 ORM 元数据，供 Alembic 与测试建表使用。

位于 bootstrap（唯一允许依赖全部模块的层）：导入本模块即注册所有表定义。
生产 schema 变更走 Alembic 迁移链；此入口仅用于迁移 autogenerate 与测试建表。
"""

from __future__ import annotations

from sqlalchemy import MetaData

from owli_parser.infrastructure.crosscut_models import (  # noqa: F401
    AuditLogRow,
    IdempotencyRecordRow,
    OutboxEventRow,
)
from owli_parser.infrastructure.db import Base
from owli_parser.modules.access import models as access_models  # noqa: F401
from owli_parser.modules.artifacts import models as artifacts_models  # noqa: F401
from owli_parser.modules.catalog import models as catalog_models  # noqa: F401
from owli_parser.modules.chat import models as chat_models  # noqa: F401
from owli_parser.modules.configuration import models as configuration_models  # noqa: F401
from owli_parser.modules.identity import models as identity_models  # noqa: F401
from owli_parser.modules.ingestion import models as ingestion_models  # noqa: F401
from owli_parser.modules.jobs import models as jobs_models  # noqa: F401
from owli_parser.modules.knowledge import models as knowledge_models  # noqa: F401


def get_metadata() -> MetaData:
    return Base.metadata
