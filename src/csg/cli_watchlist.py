"""观察池与持仓管理。

**这是整套监控系统的入口。**

系统的所有提醒都建立在「你写下的假设」之上：
- 财报发布时，逐条核对证伪条件
- 股价异动时，把当初的判断摆到你面前问「还成立吗」

没有假设，`falsification` 字段是空的，所有提醒就只是一堆数字，
你还得从头再想一遍——那系统就没有存在的意义。

**写不出证伪条件，通常说明还没真正想清楚。** 那本身就是有价值的发现，
此时正确的动作是继续研究，而不是先买了再说。
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

import pandas as pd
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from csg.storage import open_db

watch_app = typer.Typer(help="观察池与持仓（系统监控的对照物）", no_args_is_help=True)
console = Console()
DB_PATH = "data/csg.duckdb"


@watch_app.command("add")
def add(
    code: Annotated[str, typer.Argument(help="股票代码")],
    tier: Annotated[str, typer.Option(help="watch=观察池 / holding=持仓")] = "watch",
    thesis: Annotated[str, typer.Option(help="买入理由：为什么这是门好生意")] = "",
    assumptions: Annotated[str, typer.Option(help="核心假设，用；分隔")] = "",
    falsification: Annotated[str, typer.Option(help="证伪条件，用；分隔")] = "",
    target: Annotated[float, typer.Option(help="目标价 / 合理买入价")] = 0.0,
    notes: str = "",
) -> None:
    """加入观察池或持仓。

    **证伪条件是必填的。** 它是 L6 监控的直接输入——
    没有它，财报发布时系统无从核对，价格异动时无从对照。
    """
    if not falsification.strip():
        console.print(
            "[red]证伪条件不能为空[/red]\n"
            "  它回答的是「出现什么，说明我赌错了」。\n"
            "  写不出来通常意味着还没想清楚——那正是继续研究的信号，"
            "而不是先买了再说。\n\n"
            "  示例：连续两季营收增速低于10%；毛利率跌破15%；出现全行业价格战"
        )
        raise typer.Exit(1)

    with open_db(DB_PATH) as db:
        stock = db.query("SELECT name FROM stock_basic WHERE code = ?", [code])
        if stock.empty:
            console.print(f"[red]未找到股票 {code}[/red]")
            raise typer.Exit(1)
        name = stock["name"].iloc[0]

        db.conn.execute(
            "INSERT OR REPLACE INTO watchlist "
            "(code, added_at, tier, thesis, core_assumptions, falsification, "
            " target_price, notes) VALUES (?,?,?,?,?,?,?,?)",
            [code, dt.date.today(), tier, thesis or None, assumptions or None,
             falsification, target or None, notes or None])

    console.print(f"[green]✓ {code} {name} 已加入{'持仓' if tier == 'holding' else '观察池'}[/green]")
    conditions = [c.strip() for c in falsification.split("；") if c.strip()]
    console.print(f"[dim]系统将监控 {len(conditions)} 条证伪条件[/dim]")


@watch_app.command("list")
def list_all() -> None:
    """查看观察池与持仓。"""
    with open_db(DB_PATH, read_only=True) as db:
        df = db.query(
            "SELECT w.*, b.name FROM watchlist w "
            "LEFT JOIN stock_basic b ON b.code = w.code ORDER BY w.tier, w.code")

    if df.empty:
        console.print(
            "[yellow]观察池为空[/yellow]\n"
            "[dim]系统的所有监控都建立在你写下的假设之上。"
            "先用 csg watch add 加入标的。[/dim]")
        return

    for _, r in df.iterrows():
        body = []
        if r["thesis"]:
            body.append(f"[bold]买入理由[/bold]\n{r['thesis']}")
        if r["core_assumptions"]:
            items = [c.strip() for c in str(r["core_assumptions"]).split("；") if c.strip()]
            body.append("[bold]核心假设[/bold]\n" +
                        "\n".join(f"  {i}. {c}" for i, c in enumerate(items, 1)))
        if r["falsification"]:
            items = [c.strip() for c in str(r["falsification"]).split("；") if c.strip()]
            body.append("[bold]证伪条件[/bold]（系统按此监控）\n" +
                        "\n".join(f"  {i}. {c}" for i, c in enumerate(items, 1)))
        # pandas 读出的空值是 NaN 而非 None，需显式判空
        if pd.notna(r["target_price"]) and r["target_price"]:
            body.append(f"[bold]目标价[/bold] {r['target_price']}")

        console.print(Panel(
            "\n\n".join(body) or "[dim]未填写[/dim]",
            title=f"{r['code']} {r['name']} · "
                  f"{'持仓' if r['tier'] == 'holding' else '观察池'}",
            border_style="cyan" if r["tier"] == "holding" else "blue"))


@watch_app.command("remove")
def remove(code: str) -> None:
    """移出观察池。"""
    with open_db(DB_PATH) as db:
        db.conn.execute("DELETE FROM watchlist WHERE code = ?", [code])
    console.print(f"[green]✓ {code} 已移出[/green]")


@watch_app.command("facts")
def facts(codes: Annotated[list[str], typer.Argument(help="股票代码，可多个")]) -> None:
    """展示标的的关键事实 —— 写假设前先看数据。

    系统整理事实，判断由你做。这里列出的都是可验证的客观数字，
    不含任何推断或建议。
    """
    from csg.analysis import flags, metrics

    with open_db(DB_PATH, read_only=True) as db:
        panel = metrics.load_pit_panel(db, dt.date.today(), codes=codes)
        if panel.empty:
            console.print("[yellow]无财务数据，请先采集[/yellow]")
            raise typer.Exit()
        snap = metrics.latest_snapshot(
            flags.evaluate(metrics.compute_ratios(panel)))

        table = Table(title="关键事实（最新已披露）")
        for col in ["代码", "名称", "最新价", "PE", "PB", "报告期",
                    "ROE", "毛利率", "现金流/净利", "营收同比", "利润同比", "红旗"]:
            table.add_column(col, overflow="fold")

        for _, r in snap.iterrows():
            name = db.query("SELECT name FROM stock_basic WHERE code=?",
                            [r["code"]])["name"].iloc[0]
            px = db.query("SELECT close FROM daily_quote WHERE code=? "
                          "ORDER BY trade_date DESC LIMIT 1", [r["code"]])
            val = db.query("SELECT pe_ttm, pb FROM daily_basic WHERE code=? "
                           "AND pe_ttm IS NOT NULL ORDER BY trade_date DESC LIMIT 1",
                           [r["code"]])

            def f(v, pctfmt=False):
                if pd.isna(v):
                    return "—"
                return f"{v:.1%}" if pctfmt else f"{v:.2f}"

            table.add_row(
                r["code"], name,
                f(px["close"].iloc[0]) if not px.empty else "—",
                f(val["pe_ttm"].iloc[0]) if not val.empty else "—",
                f(val["pb"].iloc[0]) if not val.empty else "—",
                str(r["report_period"])[:10],
                f(r["roe_ttm"], True), f(r["gross_margin_ttm"], True),
                f(r["cfo_to_ni"]), f(r["revenue_yoy"], True),
                f(r["profit_yoy"], True),
                r["flag_names"] or "无")
        console.print(table)
        console.print(
            "[dim]以上为事实。买入理由、核心假设、证伪条件需要你的判断——"
            "系统不代劳。[/dim]")
