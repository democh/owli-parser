#!/usr/bin/env bash
# owli-parser 运维演练脚本（M5-4）。
#
# 设计原则：
# - 默认命令 check / rollback-check 只读；
# - backup 写独立备份目录（PG 自定义格式 + artifacts 归档 + manifest）；
# - restore-drill 只恢复到一个以 owli_drill_ 开头的临时数据库，不触碰生产库/卷；
# - 任何 destructive 操作都必须显式传参，且演练库名带前缀保护。
#
# 用法：
#   scripts/ops_drill.sh check
#   scripts/ops_drill.sh backup [OUTPUT_DIR]
#   scripts/ops_drill.sh restore-drill BACKUP_DIR [DRILL_DB]
#   scripts/ops_drill.sh rollback-check
#
# 环境变量：
#   COMPOSE_FILE（默认 deploy/compose.yaml）、PROFILE（默认 base）
#   API_PORT（默认 8000）、BACKUP_ROOT（默认 ./backups）
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE_FILE="${COMPOSE_FILE:-deploy/compose.yaml}"
PROFILE="${PROFILE:-base}"
API_PORT="${API_PORT:-8000}"
BACKUP_ROOT="${BACKUP_ROOT:-./backups}"
DRILL_DB_PREFIX="owli_drill_"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

compose() { docker compose -f "$COMPOSE_FILE" --profile "$PROFILE" "$@"; }

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "缺少命令：$1" >&2
    exit 1
  }
}

psql_scalar() {
  local db="$1"
  local sql="$2"
  compose exec -T postgres psql -U owli -d "$db" -tAc "$sql" | tr -d '[:space:]'
}

sha256_of() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    echo "unavailable"
  fi
}

http_check() {
  local path="$1"
  if ! command -v curl >/dev/null 2>&1; then
    echo "（跳过 $path：宿主机无 curl）"
    return 0
  fi
  curl -fsS "http://127.0.0.1:${API_PORT}${path}"
  echo
}

usage() {
  cat <<'EOF'
用法: scripts/ops_drill.sh <command>

  check                                   只读检查 compose/服务/健康/迁移/备份目录
  backup [OUTPUT_DIR]                     备份 PG + artifacts + manifest（默认 ./backups/<UTC>）
  restore-drill BACKUP_DIR [DRILL_DB]     恢复到 owli_drill_* 临时库并校验（不触碰生产库）
  rollback-check                          只读检查镜像/服务/迁移版本，输出回滚前置信息

环境变量: COMPOSE_FILE PROFILE API_PORT BACKUP_ROOT
EOF
}

cmd_check() {
  require docker
  echo "[1/6] compose 配置校验"
  compose config -q
  echo "  OK：$COMPOSE_FILE（profile=$PROFILE）"

  echo "[2/6] 服务状态"
  compose ps

  echo "[3/6] 健康检查"
  http_check /healthz
  http_check /readyz

  echo "[4/6] 指标前 20 行（/metrics）"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "http://127.0.0.1:${API_PORT}/metrics" | sed -n '1,20p'
  else
    echo "  （跳过：宿主机无 curl）"
  fi

  echo "[5/6] 迁移版本"
  psql_scalar owli "select version_num from alembic_version" || true

  echo "[6/6] 备份目录"
  mkdir -p "$BACKUP_ROOT"
  echo "  $BACKUP_ROOT 下现有 $(
    find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' '
  ) 个备份目录"
}

cmd_backup() {
  require docker
  local out="${1:-$BACKUP_ROOT/$STAMP}"
  mkdir -p "$out"

  echo "[1/3] pg_dump（custom 格式）→ $out/db.dump"
  compose exec -T postgres pg_dump -U owli -d owli -Fc >"$out/db.dump"

  echo "[2/3] artifacts 归档（容器内 Python tarfile，避免依赖 tar/GNU 差异）"
  compose exec -T api python - <<'PY' >"$out/artifacts.tgz"
import sys
import tarfile

with tarfile.open(fileobj=sys.stdout.buffer, mode="w:gz") as archive:
    archive.add("/data/artifacts", arcname="artifacts")
PY

  echo "[3/3] 写入 manifest"
  local head
  head="$(psql_scalar owli "select version_num from alembic_version" || echo unknown)"
  {
    echo "created_at=$STAMP"
    echo "compose_file=$COMPOSE_FILE"
    echo "profile=$PROFILE"
    echo "migration_head=$head"
    echo "db_dump_sha256=$(sha256_of "$out/db.dump")"
    echo "db_dump_bytes=$(wc -c <"$out/db.dump" | tr -d ' ')"
    echo "artifacts_sha256=$(sha256_of "$out/artifacts.tgz")"
    echo "artifacts_bytes=$(wc -c <"$out/artifacts.tgz" | tr -d ' ')"
    echo "--- images ---"
    compose images || true
  } >"$out/manifest.txt"

  echo "备份完成：$out"
  sed -n '1,12p' "$out/manifest.txt"
}

cmd_restore_drill() {
  require docker
  local backup="${1:-}"
  local db="${2:-${DRILL_DB_PREFIX}${STAMP}}"
  if [[ -z "$backup" || ! -f "$backup/db.dump" || ! -f "$backup/artifacts.tgz" ]]; then
    echo "用法: scripts/ops_drill.sh restore-drill BACKUP_DIR [DRILL_DB]" >&2
    echo "备份目录必须包含 db.dump 与 artifacts.tgz" >&2
    exit 2
  fi
  case "$db" in
    "${DRILL_DB_PREFIX}"*) ;;
    *)
      echo "拒绝：演练库名必须以 ${DRILL_DB_PREFIX} 开头（当前：$db）" >&2
      exit 2
      ;;
  esac

  local started
  started="$(date +%s)"

  echo "[1/5] 重建演练库 $db"
  compose exec -T postgres dropdb -U owli --if-exists --force "$db"
  compose exec -T postgres createdb -U owli "$db"

  echo "[2/5] pg_restore"
  compose exec -T postgres pg_restore -U owli -d "$db" --no-owner --no-privileges <"$backup/db.dump"

  echo "[3/5] 数据校验"
  local documents versions chunks head
  documents="$(psql_scalar "$db" "select count(*) from documents")"
  versions="$(psql_scalar "$db" "select count(*) from doc_versions")"
  chunks="$(psql_scalar "$db" "select count(*) from chunks")"
  head="$(psql_scalar "$db" "select version_num from alembic_version")"
  echo "  documents=$documents versions=$versions chunks=$chunks migration_head=$head"

  echo "[4/5] artifacts 归档校验（只读计数，不落生产卷）"
  local artifact_stats
  artifact_stats="$(
    compose exec -T api python -c "
import sys, tarfile
archive = tarfile.open(fileobj=sys.stdin.buffer, mode='r:gz')
members = archive.getmembers()
print(len(members), sum(item.size for item in members))
" <"$backup/artifacts.tgz"
  )"
  echo "  artifacts entries/bytes: $artifact_stats"

  local elapsed=$(( $(date +%s) - started ))
  echo "[5/5] 演练完成：RTO=${elapsed}s"
  echo "  演练库保留供检查：$db"
  echo "  清理：docker compose -f $COMPOSE_FILE --profile $PROFILE exec -T postgres dropdb -U owli --force $db"
}

cmd_rollback_check() {
  require docker
  echo "[1/4] 当前镜像"
  compose images
  echo "[2/4] 服务状态"
  compose ps
  echo "[3/4] 迁移版本（回滚前必须确认迁移可逆或已备份）"
  psql_scalar owli "select version_num from alembic_version" || true
  echo "[4/4] 健康检查"
  http_check /healthz
  http_check /readyz
  echo
  echo "回滚前置检查清单："
  echo "  1) 已执行 scripts/ops_drill.sh backup"
  echo "  2) 目标镜像 tag 已记录（compose images）"
  echo "  3) 新迁移遵循 expand/contract；破坏性变更只能恢复备份，不做 downgrade"
  echo "  4) 在途任务按冻结快照执行，回滚只影响新任务"
}

case "${1:-help}" in
  check) cmd_check ;;
  backup) shift; cmd_backup "${1:-}" ;;
  restore-drill) shift; cmd_restore_drill "${1:-}" "${2:-}" ;;
  rollback-check) cmd_rollback_check ;;
  help | -h | --help) usage ;;
  *)
    usage
    exit 2
    ;;
esac
