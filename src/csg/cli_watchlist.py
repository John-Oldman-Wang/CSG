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
import hashlib
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


@watch_app.command("report")
def add_manual_report(
    code: str,
    date: Annotated[str, typer.Option(help="发布日 YYYY-MM-DD")],
    institution: Annotated[str, typer.Option(help="机构，如 中金公司")],
    rating: Annotated[str, typer.Option(help="评级")] = "",
    target: Annotated[float, typer.Option(help="目标价")] = 0.0,
    eps: Annotated[str, typer.Option(help='盈利预测，如 "2026:2.10, 2027:2.85"')] = "",
    thesis: Annotated[str, typer.Option(help="核心逻辑（看多理由）")] = "",
    risks: Annotated[str, typer.Option(help="风险提示 —— 本命令最重要的字段")] = "",
    note: Annotated[str, typer.Option(help="你自己的看法")] = "",
) -> None:
    """手录一份研报的要点（来自券商 App，如中金财富）。

    **用途不是回测，是决策支持。** 批量采集拿不到中信/中金/国君这几家
    （它们的研究是卖给付费客户的产品），但你自己开户后能读到。
    盯 6 只股票每年也就十几份，手录完全可持续；而回测需要几千条，
    手录不可能——两个场景对数据量的要求差两个数量级。

    `--risks` 是最该认真填的一项：研报末尾的风险提示是**唯一由看多方
    自己写下的看空理由**，正是 AI 契约里「反方论证」与「认知缺口」
    两项的素材。那两项不允许为空，而让模型凭空生成远不如用分析师
    自己列的清单。

    ⚠️ 只录你读过的要点，**不要存原文**。券商研究服务通常约定
    仅供本人参考，摘录判断材料自用是一回事，复制存档是另一回事。
    """
    if not risks.strip():
        console.print("[yellow]提示：未填 --risks。"
                      "风险提示是本表最有价值的字段，建议补上[/yellow]")

    pub = dt.date.fromisoformat(date)
    rid = hashlib.md5(f"{code}|{pub}|{institution}".encode()).hexdigest()[:16]

    with open_db(DB_PATH) as db:
        exists = db.query("SELECT code FROM watchlist WHERE code = ?", [code])
        if exists.empty:
            console.print(f"[yellow]{code} 不在观察池中——"
                          f"手录研报的意义在于盯你真正关心的标的[/yellow]")
        db.conn.execute(
            """INSERT OR REPLACE INTO manual_report
               (report_id, code, publish_date, institution, rating,
                target_price, eps_forecast, thesis, risks, my_note)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [rid, code, pub, institution, rating or None,
             target or None, eps or None, thesis or None,
             risks or None, note or None])
    console.print(f"[green]已录入[/green] {code} {pub} {institution}"
                  f"{f' 目标价 {target}' if target else ''}")


@watch_app.command("reports")
def list_manual_reports(code: str = "") -> None:
    """列出手录的研报要点。"""
    with open_db(DB_PATH) as db:
        sql = ("SELECT m.publish_date 发布日, m.code 代码, b.name 名称, "
               "m.institution 机构, m.rating 评级, m.target_price 目标价, "
               "m.risks 风险提示 FROM manual_report m "
               "LEFT JOIN stock_basic b ON b.code = m.code")
        params: list = []
        if code:
            sql += " WHERE m.code = ?"
            params.append(code)
        df = db.query(sql + " ORDER BY m.publish_date DESC", params)
    if df.empty:
        console.print("[dim]尚无手录研报。用 csg watch report 添加[/dim]")
        return
    for _, r in df.iterrows():
        console.print(f"\n[bold]{str(r['发布日'])[:10]} {r['机构']}[/bold] "
                      f"{r['代码']} {r['名称'] or ''} "
                      f"{r['评级'] or ''}"
                      f"{f'  目标价 {r["目标价"]:.2f}' if r['目标价'] else ''}")
        if r["风险提示"]:
            console.print(f"  [yellow]风险[/yellow] {r['风险提示']}")
