#!/usr/bin/env bash
# owli-parser 跨平台开发入口（macOS/Linux）。所有命令在仓库根目录执行。
# 通过 uv 管理 Python；不在脚本中重复业务逻辑。
set -euo pipefail
cd "$(dirname "$0")/.."

cmd="${1:-help}"

case "$cmd" in
  sync)
    # 安装后端依赖（含开发工具与 CPU 解析器）
    uv sync --project backend --extra cpu
    ;;
  migrate)
    uv run --project backend owli db upgrade
    ;;
  bootstrap)
    # 平台管理员初始化；密码经 OWLI_BOOTSTRAP_ADMIN_PASSWORD 或交互输入
    uv run --project backend owli admin bootstrap --username "${OWLI_ADMIN_USERNAME:-admin}"
    ;;
  serve)
    # 开发模式 API（本地默认 sqlite + 未配置 broker 时仅 API 可用）
    uv run --project backend owli serve --reload
    ;;
  worker)
    uv run --project backend owli worker --pool cpu
    ;;
  scheduler)
    uv run --project backend owli scheduler
    ;;
  check)
    uv run --project backend ruff check backend
    uv run --project backend ruff format --check backend
    uv run --project backend mypy --config-file backend/pyproject.toml backend/src
    ;;
  test)
    uv run --project backend pytest -c backend/pyproject.toml backend/tests
    ;;
  config)
    uv run --project backend owli config validate
    ;;
  drill)
    # 运维演练（默认只读；备份/恢复演练见 docs/operations.md）
    shift
    exec scripts/ops_drill.sh "$@"
    ;;
  web)
    pnpm --dir frontend install
    pnpm --dir frontend dev
    ;;
  *)
    echo "用法: scripts/dev.sh <sync|migrate|bootstrap|serve|worker|scheduler|check|test|config|drill|web>"
    ;;
esac
