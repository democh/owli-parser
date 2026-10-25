"""owli CLI：config validate / db upgrade / admin bootstrap / serve / worker / scheduler。

- CLI 自行解析已配置资源与迁移路径，不依赖当前 shell 目录。
- admin bootstrap 从受保护输入读取密码，不提供默认 admin/admin；
  命令重复执行不得重置已有密码。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import typer

app = typer.Typer(
    name="owli",
    help="owli-parser 命令行（配置、迁移、管理、服务进程）",
    no_args_is_help=True,
    add_completion=False,
)

config_app = typer.Typer(help="配置校验", no_args_is_help=True)
db_app = typer.Typer(help="数据库迁移", no_args_is_help=True)
admin_app = typer.Typer(help="管理命令", no_args_is_help=True)
contracts_app = typer.Typer(help="契约快照导出与检查", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(admin_app, name="admin")
app.add_typer(contracts_app, name="contracts")


def _default_contracts_dir() -> Path:
    """仓库根 contracts/（backend 的上级目录）。"""
    return _backend_dir().parent / "contracts"


def _backend_dir() -> Path:
    """返回 backend 项目根（pyproject 所在目录）。"""
    return Path(__file__).resolve().parents[3]


# 回归集骨架：占位 UUID 需替换为真实版本（reference/hypothesis 均为已发布或已解析版本）
_REGRESSION_TEMPLATE = {
    "cases": [
        {
            "reference_version_id": "00000000-0000-0000-0000-000000000000",
            "hypothesis_version_id": "00000000-0000-0000-0000-000000000000",
            "groups": {"format": "pdf", "language": "zh"},
        }
    ],
    "expectations": {"max_cer": 0.05, "min_reading_order": 0.9},
}


@config_app.command("validate")
def config_validate() -> None:
    """加载并校验启动配置；非法配置以非零退出码失败且不泄密。"""
    from owli_parser.bootstrap.settings import load_boot_settings, validate_config

    settings = load_boot_settings()
    warnings = validate_config(settings)
    typer.echo(f"配置校验通过：environment={settings.app.environment}")
    for warning in warnings:
        typer.echo(f"  [warning] {warning}")


@contracts_app.command("export")
def contracts_export_cmd(
    outdir: Path | None = typer.Option(default=None, help="输出目录（默认仓库根 contracts/）"),
) -> None:
    """导出版本化契约快照：OpenAPI 与 IR/事件 JSON Schema。"""
    from owli_parser.bootstrap.contracts_export import export_contracts

    target = outdir or _default_contracts_dir()
    for path in export_contracts(target):
        typer.echo(f"已写出 {path}")


@contracts_app.command("check")
def contracts_check_cmd(
    outdir: Path | None = typer.Option(default=None, help="快照目录（默认仓库根 contracts/）"),
) -> None:
    """检查契约快照与当前代码一致；不一致以非零退出码失败（CI 门禁）。"""
    from owli_parser.bootstrap.contracts_export import check_contracts

    target = outdir or _default_contracts_dir()
    diffs = check_contracts(target)
    if diffs:
        for diff in diffs:
            typer.echo(f"[drift] {diff}")
        raise typer.Exit(code=1)
    typer.echo("契约快照与代码一致")


@db_app.command("upgrade")
def db_upgrade(revision: str = typer.Argument(default="head")) -> None:
    """执行 Alembic 迁移（统一版本链；部署先迁移再发布应用）。"""
    from alembic import command
    from alembic.config import Config

    backend = _backend_dir()
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    # 保证可导入 src 布局
    sys.path.insert(0, str(backend / "src"))
    command.upgrade(config, revision)
    typer.echo(f"数据库已升级至 {revision}")


@admin_app.command("bootstrap")
def admin_bootstrap(
    username: str = typer.Option(..., prompt=True, help="平台管理员用户名"),
    display_name: str | None = typer.Option(default=None),
) -> None:
    """创建平台管理员；密码从环境变量或交互输入读取（不回显、不落日志）。"""
    from owli_parser.bootstrap.container import Container

    password = os.environ.get("OWLI_BOOTSTRAP_ADMIN_PASSWORD")
    if not password:
        password = typer.prompt("请输入管理员密码", hide_input=True, confirmation_prompt=True)
    container = Container()

    async def _run() -> None:
        profile = await container.identity.bootstrap_admin(username, password, display_name)
        typer.echo(f"管理员就绪：{profile.username} (id={profile.id})")
        await container.dispose()

    asyncio.run(_run())


@app.command("serve")
def serve(
    host: str | None = typer.Option(default=None),
    port: int | None = typer.Option(default=None),
    reload: bool = typer.Option(default=False, help="开发模式热重载"),
) -> None:
    """启动 API 服务（uvicorn）。"""
    import uvicorn

    from owli_parser.bootstrap.settings import load_boot_settings

    settings = load_boot_settings()
    bind_host = host or settings.app.bind_host
    bind_port = port or settings.app.bind_port
    typer.echo(f"启动 API：http://{bind_host}:{bind_port}")
    uvicorn.run(
        "owli_parser.bootstrap.app:create_app",
        factory=True,
        host=bind_host,
        port=bind_port,
        reload=reload,
        log_config=None,
    )


@app.command("worker")
def worker(pool: str = typer.Option(default="cpu", help="cpu 或 gpu")) -> None:
    """启动解析 worker。"""
    from owli_parser.bootstrap.worker import run_worker

    run_worker(pool)


@app.command("eval-retrieval")
def eval_retrieval(
    file: Path = typer.Option(
        ..., "--file", exists=True, dir_okay=False, help="黄金集 JSON（cases + expectations）"
    ),
    name: str = typer.Option("retrieval-default", help="黄金集名称（同名幂等更新）"),
    adapter_ref: str = typer.Option("lexical-bm25", help="评估的检索管道标识"),
) -> None:
    """运行检索黄金集评估（Recall@K/nDCG@K/MRR）；未达基线以非零退出码失败。

    cases 需携带 tenant_id（本地/CI 数据固有属性）与 dataset_ids、relevant。
    """
    import json

    from owli_parser.bootstrap.container import Container
    from owli_parser.modules.retrieval.golden_runner import GoldenRunner

    payload = json.loads(file.read_text(encoding="utf-8"))
    cases = payload["cases"]
    expectations = payload.get("expectations", {})
    container = Container()

    async def _run() -> int:
        try:
            golden = await container.knowledge.upsert_golden_set(
                name=name, cases=cases, expectations=expectations
            )

            from owli_parser.bootstrap.quality_runner import make_retrieval_search_fn

            if not golden.tenant_id:
                # CLI 评估必须落到某个租户的检索上下文（case 数据归属）
                raise typer.BadParameter("黄金集缺少 tenant_id（CLI 评估必须显式声明）")
            search_fn = make_retrieval_search_fn(container, golden.tenant_id)

            runner = GoldenRunner(container.knowledge, container.clock)
            run = await runner.run(golden_set=golden, search_fn=search_fn, adapter_ref=adapter_ref)
            typer.echo(f"run_id={run.run_id} passed={run.passed}")
            for metric, value in sorted(run.aggregate.items()):
                typer.echo(f"  {metric}: {value:.4f}")
            for violation in run.violations:
                typer.echo(f"  [violation] {violation}")
            return 0 if run.passed else 1
        finally:
            await container.dispose()

    raise typer.Exit(code=asyncio.run(_run()))


@app.command("compare-ir")
def compare_ir(
    tenant: str = typer.Option(..., "--tenant", help="租户 UUID（运维通道需显式声明）"),
    reference: str = typer.Option(..., "--reference", help="基准版本 UUID"),
    hypothesis: str = typer.Option(..., "--hypothesis", help="候选版本 UUID"),
    adapter_ref: str = typer.Option("parsing-compare", help="运行标识（记录到 golden_runs）"),
    golden_set: str = typer.Option("parsing-compare", help="落库使用的黄金集名（同名幂等）"),
    max_cer: float | None = typer.Option(default=None, help="CER 上限（超限即失败退出）"),
    min_reading_order: float | None = typer.Option(default=None, help="阅读顺序下限"),
) -> None:
    """对比两个版本的解析质量（IR 指标），未达阈值以非零退出码失败并落 golden_runs。"""
    import uuid as uuid_module

    from owli_parser.bootstrap.container import Container
    from owli_parser.bootstrap.quality_runner import run_parsing_comparison

    container = Container()

    async def _run() -> int:
        try:
            outcome = await run_parsing_comparison(
                container,
                tenant_id=uuid_module.UUID(tenant),
                reference_version_id=uuid_module.UUID(reference),
                hypothesis_version_id=uuid_module.UUID(hypothesis),
                adapter_ref=adapter_ref,
                golden_set_name=golden_set,
                max_cer=max_cer,
                min_reading_order=min_reading_order,
            )
            comparison = outcome.comparison
            typer.echo(f"run_id={outcome.run_id} passed={outcome.passed}")
            typer.echo(f"  text_cer: {comparison.text_cer:.6f}")
            typer.echo(f"  reading_order: {comparison.reading_order:.6f}")
            table_text = f"{comparison.table_f1:.6f}" if comparison.table_f1 is not None else "—（无表格对）"
            typer.echo(f"  table_structure_f1: {table_text}")
            location_text = (
                f"{comparison.location_iou:.6f}" if comparison.location_iou is not None else "—（无定位对）"
            )
            typer.echo(f"  location_iou: {location_text}")
            typer.echo(
                f"  blocks: reference={comparison.reference_blocks} hypothesis={comparison.hypothesis_blocks}"
            )
            for violation in outcome.violations:
                typer.echo(f"  [violation] {violation}")
            return 0 if outcome.passed else 1
        finally:
            await container.dispose()

    raise typer.Exit(code=asyncio.run(_run()))


@app.command("parsing-regression")
def parsing_regression(
    file: Path | None = typer.Option(
        None, "--file", exists=True, dir_okay=False, help="回归集 JSON（cases + expectations）"
    ),
    tenant: str | None = typer.Option(None, "--tenant", help="租户 UUID（运维通道需显式声明）"),
    name: str = typer.Option("parsing-regression", help="回归集名称（同名幂等更新）"),
    adapter_ref: str = typer.Option("parsing-compare", help="运行标识（记录到 golden_runs）"),
    template: Path | None = typer.Option(
        None, "--template", help="生成 cases.json 骨架到该路径后退出（占位 UUID 需替换）"
    ),
) -> None:
    """批量解析回归（版本对逐 case 对比 → 宏平均 + 阈值）；退出码即 CI 护栏。

    cases: [{reference_version_id, hypothesis_version_id, groups?}, ...]；
    expectations: {max_cer?, min_reading_order?} 对聚合值判定。
    """
    import json
    import uuid as uuid_module

    if template is not None:
        if template.exists():
            raise typer.BadParameter(f"{template} 已存在，拒绝覆盖")
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text(
            json.dumps(_REGRESSION_TEMPLATE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        typer.echo(f"已写出回归集模板：{template}（请替换占位 UUID 后再运行）")
        return
    if file is None:
        raise typer.BadParameter("需提供 --file（或用 --template 生成骨架）")
    if tenant is None:
        raise typer.BadParameter("--tenant 为必填")

    from owli_parser.bootstrap.container import Container
    from owli_parser.bootstrap.quality_runner import run_parsing_regression

    payload = json.loads(file.read_text(encoding="utf-8"))
    cases = payload.get("cases") or []
    if not cases:
        raise typer.BadParameter("cases 不能为空")
    expectations = payload.get("expectations") or {}
    container = Container()

    async def _run() -> int:
        try:
            golden = await container.knowledge.upsert_golden_set(
                name=name, cases=cases, expectations=expectations
            )
            outcome = await run_parsing_regression(
                container,
                tenant_id=uuid_module.UUID(tenant),
                golden_set=golden,
                max_cer=expectations.get("max_cer"),
                min_reading_order=expectations.get("min_reading_order"),
                adapter_ref=adapter_ref,
            )
            typer.echo(f"run_id={outcome.run_id} passed={outcome.passed} cases={len(outcome.case_results)}")
            for key, value in sorted(outcome.aggregate.items()):
                typer.echo(f"  {key}: {value:.6f}")
            for violation in outcome.violations:
                typer.echo(f"  [violation] {violation}")
            return 0 if outcome.passed else 1
        finally:
            await container.dispose()

    raise typer.Exit(code=asyncio.run(_run()))


@app.command("scheduler")
def scheduler() -> None:
    """启动 outbox 派发与租约回收循环。"""
    from owli_parser.bootstrap.scheduler import run_scheduler

    asyncio.run(run_scheduler())


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
