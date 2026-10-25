# owli-parser API/worker/scheduler 镜像（CPU 基线）。
# - 锁定 Python/uv 大版本；构建先 `uv sync --locked` 验证锁文件再安装明确 extras。
# - 生产依赖不使用 --all-extras；测试工具与 secret 不打入镜像。
# - 构建上下文为仓库根目录：docker build -f deploy/backend.Dockerfile .
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    OWLI_APP__BIND_HOST=0.0.0.0

RUN pip install --no-cache-dir "uv>=0.10,<1"

WORKDIR /app

# 依赖声明与锁文件（利用层缓存）
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./backend/
COPY backend/README.md ./backend/

WORKDIR /app/backend
# 验证锁文件与声明一致（--locked），仅安装生产依赖与明确 extras
ARG EXTRAS="--extra cpu --extra storage-s3 --extra storage-oss"
RUN uv sync --locked --no-default-groups $EXTRAS

# 应用代码与迁移
COPY backend/src ./src
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./

# 数据目录（Compose 挂载卷覆盖）
RUN mkdir -p /data/artifacts

# 非 root 运行
RUN useradd --create-home --uid 10001 owli \
    && chown -R owli:owli /app /data
USER owli

EXPOSE 8000

# 健康检查：liveness 只反映进程存活
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"

# 默认以 API 启动；worker/scheduler 由 Compose 覆盖 command
CMD ["uv", "run", "--no-sync", "owli", "serve"]
