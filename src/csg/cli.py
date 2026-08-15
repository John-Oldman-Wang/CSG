"""CSG 命令行入口。

采集类命令均为**幂等**：中断后重跑自动从水位续传，不会重复拉取。
数据源存在限流，全量采集耗时以小时计，可随时 Ctrl+C 中断，
稍后再跑即可接上。
"""

from __future__ import annotations

import datetime as dt
import logging
import warnings
from typing import Annotated

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

warnings.filterwarnings("ignore")

from csg import universe
from csg.analysis import flags, metrics
from csg.cli_events import events_app
from csg.pipeline import Ingestor
from csg.storage import open_db

app = typer.Typer(help="CSG 个人 A 股投研系统", no_args_is_help=True)
sync_app = typer.Typer(help="数据采集（幂等，可中断续传）", no_args_is_help=True)
app.add_typer(sync_app, name="sync")
app.add_typer(events_app, name="ev")

console = Console()
DB_PATH = "data/csg.duckdb"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def _pool_codes(db, limit: int | None = None) -> list[str]:
    pool = universe.resolve_universe(db)
    codes = pool["code"].tolist()
    return codes[:limit] if limit else codes


def _show(df: pd.DataFrame, title: str, max_rows: int = 30) -> None:
    if df.empty:
        console.print(f"[yellow]{title}：无数据[/yellow]")
        return
    table = Table(title=f"{title}（{len(df)} 行）", show_lines=False)
    for c in df.columns:
        table.add_column(str(c), overflow="fold")
    for _, row in df.head(max_rows).iterrows():
        table.add_row(*[
            f"{v:.3f}" if isinstance(v, float) else str(v) for v in row
        ])
    console.print(table)
    if len(df) > max_rows:
        console.print(f"[dim]… 另有 {len(df) - max_rows} 行未显示[/dim]")


# ----------------------------------------------------------------------
# sync
# ----------------------------------------------------------------------

@sync_app.command("basic")
def sync_basic(verbose: bool = True) -> None:
    """股票列表（含退市股）与行业分类。

    退市股必须入库：用今天的股票列表回测历史会剔除所有退市公司，
    产生幸存者偏差。
    """
    _setup_logging(verbose)
    with open_db(DB_PATH) as db:
        ing = Ingestor(db)
        ing.sync_stock_basic()
        ing.sync_industry()
    console.print("[green]✓ 基础信息同步完成[/green]")


@sync_app.command("quotes")
def sync_quotes(
    limit: Annotated[int, typer.Option(help="仅同步前 N 只，用于试跑")] = 0,
    start: Annotated[str, typer.Option(help="起始日期 YYYY-MM-DD")] = "2016-01-01",
    force: Annotated[bool, typer.Option(help="忽略水位，全量重拉")] = False,
    verbose: bool = True,
) -> None:
    """L1 基础池日线行情（原始价 + 复权因子）。"""
    _setup_logging(verbose)
    with open_db(DB_PATH) as db:
        codes = _pool_codes(db, limit or None)
        console.print(f"目标 {len(codes)} 只，起始 {start}")
        stats = Ingestor(db).sync_daily_quotes(
            codes, start=dt.date.fromisoformat(start), force=force
        )
    console.print(f"[green]行情完成[/green] {stats}")


@sync_app.command("financials")
def sync_financials(
    limit: Annotated[int, typer.Option(help="仅同步前 N 只，用于试跑")] = 0,
    force: Annotated[bool, typer.Option(help="忽略水位，全量重拉")] = False,
    verbose: bool = True,
) -> None:
    """L1 基础池三大报表（含 PIT 所需的披露日期）。"""
    _setup_logging(verbose)
    with open_db(DB_PATH) as db:
        codes = _pool_codes(db, limit or None)
        console.print(f"目标 {len(codes)} 只 × 3 表")
        stats = Ingestor(db).sync_financials(codes, force=force)
    console.print(f"[green]财务完成[/green] {stats}")


@sync_app.command("research")
def sync_research(
    limit: Annotated[int, typer.Option(help="仅同步前 N 只，用于试跑")] = 0,
    force: Annotated[bool, typer.Option(help="忽略水位，全量重拉")] = False,
    verbose: bool = True,
) -> None:
    """研报与盈利预测（约 202 条/只，35 分钟全量）。

    每次采集保留快照：历史盈利预测无法回溯获取，
    只能从现在开始积累一致预期的时间序列。
    """
    _setup_logging(verbose)
    with open_db(DB_PATH) as db:
        codes = _pool_codes(db, limit or None)
        console.print(f"目标 {len(codes)} 只，预计 {len(codes) * 3.5 / 60:.0f} 分钟")
        stats = Ingestor(db).sync_research(codes, force=force)
    console.print(f"[green]研报完成[/green] {stats}")


@sync_app.command("valuations")
def sync_valuations(
    limit: Annotated[int, typer.Option(help="仅同步前 N 只")] = 0,
    force: bool = False,
    verbose: bool = True,
) -> None:
    """每日估值 PE/PB/PS（baostock）。

    akshare 无稳定的历史 PE 批量接口，baostock K 线接口原生附带，
    正好填补验证② 与前端估值展示所需。
    """
    _setup_logging(verbose)
    with open_db(DB_PATH) as db:
        codes = db.query(
            "SELECT DISTINCT code FROM daily_quote ORDER BY code")["code"].tolist()
        if limit:
            codes = codes[:limit]
        console.print(f"目标 {len(codes)} 只")
        stats = Ingestor(db).sync_valuations(codes, force=force)
    console.print(f"[green]估值完成[/green] {stats}")


@sync_app.command("retry")
def sync_retry(
    dataset: Annotated[str, typer.Argument(help="数据集名，如 fin_income / daily_quote")],
    verbose: bool = True,
) -> None:
    """定向重试某数据集中失败的条目。"""
    _setup_logging(verbose)
    with open_db(DB_PATH) as db:
        ing = Ingestor(db)
        codes = ing.retry_failed(dataset)
        if not codes:
            console.print("[green]无失败条目[/green]")
            raise typer.Exit()
        console.print(f"重试 {len(codes)} 只")
        stmt_map = {"fin_income": "income", "fin_balance": "balance",
                    "fin_cashflow": "cashflow"}
        if dataset == "daily_quote":
            stats = ing.sync_daily_quotes(codes, force=True)
        elif dataset == "research_report":
            stats = ing.sync_research(codes, force=True)
        elif dataset in stmt_map:
            stats = ing.sync_financials(codes, statements=[stmt_map[dataset]],
                                        force=True)
        else:
            console.print(f"[red]未知数据集 {dataset}[/red]")
            raise typer.Exit(1)
    console.print(f"[green]完成[/green] {stats}")


# ----------------------------------------------------------------------
# 查询与分析
# ----------------------------------------------------------------------

@app.command()
def health(stale_days: int = 7) -> None:
    """数据健康检查。

    调度静默失效是本环境最危险的故障：系统看起来在跑，
    实际早已停止且无任何报错。
    """
    with open_db(DB_PATH) as db:
        _show(Ingestor(db).health_report(stale_days), "数据水位")
        counts = db.query("""
            SELECT 'stock_basic' AS 表, count(*) AS 行数 FROM stock_basic
            UNION ALL SELECT 'daily_quote', count(*) FROM daily_quote
            UNION ALL SELECT 'fin_income', count(*) FROM fin_income
            UNION ALL SELECT 'fin_balance', count(*) FROM fin_balance
            UNION ALL SELECT 'fin_cashflow', count(*) FROM fin_cashflow
        """)
        _show(counts, "入库统计")


@app.command()
def pool() -> None:
    """查看 L1 基础池构成。"""
    with open_db(DB_PATH) as db:
        p = universe.resolve_universe(db)
        summary = (p.groupby(["theme", "industry_name"])
                   .size().reset_index(name="只数"))
        _show(summary, "L1 基础池")
        console.print(f"[bold]合计 {len(p)} 只[/bold]")


@app.command("flags")
def check_flags(
    as_of: Annotated[str, typer.Option(help="评估时点 YYYY-MM-DD，默认今天")] = "",
    code: Annotated[str, typer.Option(help="限定单只股票")] = "",
    min_score: Annotated[int, typer.Option(help="仅显示分值达标的")] = 1,
) -> None:
    """在指定时点评估财务红旗。

    严格 point-in-time：只使用该时点**已经披露**的财报，
    因此可回答「在 T 时刻我能否发现问题」。
    """
    ref = dt.date.fromisoformat(as_of) if as_of else dt.date.today()
    with open_db(DB_PATH) as db:
        codes = [code] if code else _pool_codes(db)
        panel = metrics.load_pit_panel(db, ref, codes=codes)
        if panel.empty:
            console.print("[yellow]该时点无可见财务数据，请先 sync financials[/yellow]")
            raise typer.Exit()

        snap = metrics.latest_snapshot(flags.evaluate(metrics.compute_ratios(panel)))
        snap = snap[snap["flag_score"] >= min_score].sort_values(
            "flag_score", ascending=False)
        cols = ["code", "report_period", "disclosure_date",
                "flag_score", "flag_count", "flag_names"]
        _show(snap[cols], f"红旗评估 @ {ref}")


@app.command()
def sql(query: Annotated[str, typer.Argument(help="SQL 语句")]) -> None:
    """直接执行 SQL（只读探查用）。"""
    with open_db(DB_PATH, read_only=True) as db:
        _show(db.query(query), "查询结果")


# ----------------------------------------------------------------------
# 验证
# ----------------------------------------------------------------------

validate_app = typer.Typer(help="方法论验证（需先完成数据采集）", no_args_is_help=True)
app.add_typer(validate_app, name="validate")


@validate_app.command("flags")
def validate_flags(
    start: str = "2018-01-01",
    lookback: Annotated[int, typer.Option(help="事件前回溯季度数")] = 8,
    min_score: Annotated[int, typer.Option(help="判定预警的分值门槛")] = 3,
) -> None:
    """验证① 红旗规则能否提前发现暴雷。

    三个验证里结论最可信的一个：事件客观、严格 PIT、产出直接可用于
    校准 config/exclusion.yaml 的阈值。
    """
    from csg.validation import flag_backtest

    with open_db(DB_PATH) as db:
        events = flag_backtest.find_blowup_events(
            db, start=dt.date.fromisoformat(start))
        console.print(f"识别暴雷事件 [bold]{len(events)}[/bold] 起")
        if events.empty:
            raise typer.Exit()

        _show(events.groupby("event_type").size().reset_index(name="起数"), "事件类型分布")

        console.print("[dim]逐事件回溯评估中，耗时较长…[/dim]")
        warned = flag_backtest.backtest_early_warning(
            db, events, lookback_quarters=lookback)

        summary = flag_backtest.summarize(warned, min_score=min_score)
        for k, v in summary.items():
            console.print(f"  [cyan]{k}[/cyan]: {v}")

        console.print(
            "\n[yellow]注意：覆盖率必须与误报率同看。"
            "把所有股票都标红，覆盖率必然 100%。[/yellow]")


@validate_app.command("research")
def validate_research(
    view: Annotated[str, typer.Option(
        help="视角：all/rating/change/prior/density/first")] = "all",
    save: Annotated[bool, typer.Option(help="结果存入 validation_run/result")] = True,
    note: Annotated[str, typer.Option(help="本次运行备注")] = "",
) -> None:
    """验证④ 研报是否有预测力。

    发现期(2018-2022) 与验证期(2023-) 分别输出。
    判定标准：同一效应须在两期同向，仅发现期成立者视为噪音。

    注：目标价不在数据中（实测预测PE = 发布日股价/预测EPS），
    故「是否涨到目标价」无法验证，改以路径指标替代。

    结果默认落库：回测依赖当时的数据状态，数据在增长，
    同段代码下月跑出的数字会不同，无快照则无法解释差异。
    """
    from csg.validation import research_study as rs
    from csg.validation import store

    with open_db(DB_PATH) as db:
        n = db.query("SELECT count(*) AS n FROM research_report")["n"].iloc[0]
        q = db.query("SELECT count(*) AS n FROM daily_quote")["n"].iloc[0]
        console.print(f"研报 {n} 条 · 行情 {q} 行")
        if n == 0 or q == 0:
            console.print("[yellow]需先完成 sync research 与 sync quotes[/yellow]")
            raise typer.Exit()

        console.print("[dim]计算中（全部下推 DuckDB，视数据量需数分钟）…[/dim]")
        results = rs.run_full_study(db)

        wanted = {
            "rating": "评级", "change": "评级调整", "prior": "发布前走势",
            "density": "覆盖密度", "first": "首次覆盖",
        }.get(view)

        for key, table in results.items():
            if wanted and wanted not in key:
                continue
            if table is None or table.empty:
                console.print(f"[dim]{key}：样本不足[/dim]")
                continue
            _show(table, key)

        if save:
            rid = store.save_run(
                db, "research_reliability",
                params={"in_sample": [str(d) for d in rs.IN_SAMPLE],
                        "out_sample": [str(d) for d in rs.OUT_SAMPLE],
                        "horizons": list(rs.DEFAULT_HORIZONS),
                        "main_horizon": rs.MAIN_HORIZON},
                results=results, note=note)
            console.print(f"\n[green]结果已保存 run_id = {rid}[/green]")
            console.print("[dim]查看历史：csg validate runs[/dim]")

        console.print(
            "\n[yellow]判定：只有在发现期与验证期**同向**的效应才可采信。"
            "样本数过小的分组（<50）统计量不可靠。[/yellow]")


@validate_app.command("runs")
def list_validation_runs(
    vtype: Annotated[str, typer.Option(help="按验证类型过滤")] = "",
) -> None:
    """历史验证运行记录。"""
    from csg.validation import store

    with open_db(DB_PATH, read_only=True) as db:
        _show(store.list_runs(db, vtype or None), "验证运行历史")


@validate_app.command("show")
def show_validation_run(run_id: str) -> None:
    """还原某次验证的完整结果。"""
    from csg.validation import store

    with open_db(DB_PATH, read_only=True) as db:
        meta = db.query(
            "SELECT validation_type, run_at, params, data_snapshot, note "
            "FROM validation_run WHERE run_id = ?", [run_id])
        if meta.empty:
            console.print("[red]run_id 不存在[/red]")
            raise typer.Exit(1)
        r = meta.iloc[0]
        console.print(f"[bold]{r['validation_type']}[/bold]  {r['run_at']}")
        console.print(f"[dim]数据快照 {r['data_snapshot']}[/dim]\n")
        for key, table in store.load_run(db, run_id).items():
            _show(table, key)


@validate_app.command("conclude")
def add_conclusion(
    vtype: Annotated[str, typer.Option(help="验证类型")],
    finding: Annotated[str, typer.Option(help="发现的效应")],
    in_sample: Annotated[str, typer.Option(help="发现期结果摘要")],
    out_sample: Annotated[str, typer.Option(help="验证期结果摘要")],
    verdict: Annotated[str, typer.Option(help="adopted/rejected/pending")],
    run_id: Annotated[str, typer.Option(help="关联的 run_id")] = "",
    applied_to: Annotated[str, typer.Option(help="应用于哪条决策规则")] = "",
) -> None:
    """记录人工判定 —— 把回测发现转化为是否采纳。

    adopted 只应在发现期与验证期**同向**时使用。
    被否定的发现同样保留：记住哪些假设失败过，与记住哪些成立同样重要。
    """
    from csg.validation import store

    with open_db(DB_PATH) as db:
        cid = store.save_conclusion(
            db, validation_type=vtype, finding=finding, in_sample=in_sample,
            out_sample=out_sample, verdict=verdict,
            run_id=run_id or None, applied_to=applied_to)
    console.print(f"[green]结论已记录 {cid}（{verdict}）[/green]")


@validate_app.command("cycle")
def validate_cycle(
    theme: Annotated[str, typer.Option(help="主题：new_energy / ai_compute")] = "new_energy",
) -> None:
    """验证② 行业周期规律。

    核心问题：用 PE 分位筛选周期行业，是否会系统性在高点买入。
    """
    from csg.validation import cycle_study

    with open_db(DB_PATH) as db:
        industries = universe.target_industries().get(theme, [])
        if not industries:
            console.print(f"[red]未知主题 {theme}[/red]")
            raise typer.Exit(1)

        agg = cycle_study.industry_aggregate(db, industries)
        if agg.empty:
            console.print("[yellow]无数据，请先 sync financials[/yellow]")
            raise typer.Exit()

        ind = cycle_study.cycle_indicators(agg)
        cols = ["industry_name", "report_period", "companies", "gross_margin",
                "net_margin", "capex_intensity", "inventory_ratio", "revenue_yoy"]
        _show(ind[[c for c in cols if c in ind.columns]].round(3),
              f"{theme} 行业周期指标（年度）", max_rows=40)


@validate_app.command("screen")
def validate_screen(
    start: str = "2018-01-01",
    end: str = "2025-12-31",
    top_n: int = 20,
    rebalance: Annotated[int, typer.Option(help="调仓间隔月数")] = 6,
) -> None:
    """验证③ 筛选条件历史表现。

    ⚠️ 本命令验证的是**机械筛选层**，不是方法论。
    方法论中人的判断部分（护城河、证伪条件、复核结论）无法回测。
    """
    from csg.validation import screen_backtest as sb

    console.print("[yellow]⚠️ 本回测仅验证机械筛选层。"
                  "漂亮的收益率不构成对方法论的验证。[/yellow]\n")

    rule = sb.ScreenRule(
        conditions=[("roe_ttm", ">", 0.12), ("cfo_to_ni", ">", 0.6),
                    ("debt_ratio", "<", 0.65)],
        top_n=top_n,
    )
    cfg = sb.BacktestConfig(
        start=dt.date.fromisoformat(start),
        end=dt.date.fromisoformat(end),
        rebalance_months=rebalance,
    )

    with open_db(DB_PATH) as db:
        detail, summary = sb.run_backtest(db, rule, cfg)
        if not detail.empty:
            _show(detail.drop(columns=["codes"]), "分期表现")
        for k, v in summary.items():
            console.print(f"  [cyan]{k}[/cyan]: {v}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
