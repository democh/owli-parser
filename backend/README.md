# owli-parser backend

企业知识库文件解析与知识库工作台后端（模块化单体 + 独立 worker）。

- Python 3.12，使用 uv 管理（`uv.lock` 与 `.python-version` 提交版本管理）。
- 权威状态存储 PostgreSQL；本地开发可用 sqlite+aiosqlite（非生产）。
- 集中配置：`src/owli_parser/resources/defaults.yaml` 为默认值，环境覆盖通过
  `OWLI_CONFIG_FILE` 指定的 YAML 与 `OWLI_` 前缀环境变量完成；密钥只写引用。

常用命令（仓库根目录执行）：

```bash
uv sync --project backend --locked --extra cpu --extra storage-s3 --extra storage-oss
uv run --project backend --locked owli config validate
uv run --project backend --locked owli db upgrade
uv run --project backend --locked owli admin bootstrap
uv run --project backend --locked owli serve
uv run --project backend --locked owli worker --pool cpu
uv run --project backend --locked owli scheduler
uv run --project backend --locked pytest -c backend/pyproject.toml backend/tests
```
