"""事件驱动相关命令。

完整闭环：

    detect  扫描数据产生事件（幂等）
      ↓
    tasks   事件经策略转为复核任务（带 SLA）
      ↓
    notify  推送飞书卡片
      ↓
    review  人在 CLI 提交结论
      ↓
    复盘层统计及时处理率与结论正确率
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Annotated

import pandas as pd
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from csg.events import Detector, StrategyEngine
from csg.notify import FeishuNotifier, channel_for
from csg.storage import open_db

console = Console()
DB_PATH = "data/csg.duckdb"

events_app = typer.Typer(help="事件驱动：检测 / 任务 / 推送 / 复核", no_args_is_help=True)


def _show(df: pd.DataFrame, title: str, max_rows: int = 30) -> None:
    if df.empty:
        console.print(f"[yellow]{title}：无[/yellow]")
        return
    t = Table(title=f"{title}（{len(df)}）")
    for c in df.columns:
        t.add_column(str(c), overflow="fold")
    for _, r in df.head(max_rows).iterrows():
        t.add_row(*[f"{v:.3f}" if isinstance(v, float) else str(v) for v in r])
    console.print(t)


@events_app.command("detect")
def detect(
    days: Annotated[int, typer.Option(help="扫描最近 N 天")] = 30,
) -> None:
    """扫描数据产生事件。

    幂等：event_id 由事件自然键哈希得到，重复扫描不产生重复事件。
    休眠唤醒后的补跑会重扫大量历史，没有这条会瞬间刷屏。
    """
    since = dt.date.today() - dt.timedelta(days=days)
    with open_db(DB_PATH) as db:
        n = Detector(db).run_all(since=since)
        console.print(f"[green]新增事件 {n} 个[/green]")
        _show(db.query(
            "SELECT event_type AS 类型, severity AS 级别, count(*) AS 数量 "
            "FROM event GROUP BY ALL ORDER BY 数量 DESC"), "事件汇总")


@events_app.command("tasks")
def tasks(
    status: Annotated[str, typer.Option(help="pending/notified/concluded/all")] = "pending",
    overdue_only: bool = False,
) -> None:
    """待办复核任务。

    超时未处理的任务会被标出——「及时处理率」是混合派的核心防自欺指标：
    持续走低意味着在逃避面对亏损标的。
    """
    with open_db(DB_PATH) as db:
        sql = """
            SELECT t.task_id, t.code, b.name, t.severity, t.title,
                   t.status, t.due_at,
                   CASE WHEN t.due_at < current_timestamp
                         AND t.status <> 'concluded' THEN '⚠️超时' ELSE '' END AS 逾期
            FROM review_task t
            LEFT JOIN stock_basic b ON b.code = t.code
        """
        params: list = []
        conds = []
        if status != "all":
            conds.append("t.status = ?")
            params.append(status)
        if overdue_only:
            conds.append("t.due_at < current_timestamp AND t.status <> 'concluded'")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY t.severity, t.due_at"
        _show(db.query(sql, params), f"复核任务（{status}）")


@events_app.command("build")
def build_tasks() -> None:
    """把未处理的事件转换为复核任务。"""
    with open_db(DB_PATH) as db:
        created = StrategyEngine(db).process_pending_events()
        console.print(f"[green]生成任务 {len(created)} 个[/green]")
        for t in created[:10]:
            console.print(f"  [{t.severity}] {t.code} {t.title}")


@events_app.command("show")
def show_task(task_id: str) -> None:
    """查看任务详情（含系统备好的材料与待答问题）。"""
    with open_db(DB_PATH) as db:
        df = db.query(
            "SELECT t.*, b.name FROM review_task t "
            "LEFT JOIN stock_basic b ON b.code = t.code WHERE t.task_id = ?",
            [task_id])
        if df.empty:
            console.print("[red]任务不存在[/red]")
            raise typer.Exit(1)

        r = df.iloc[0]
        ctx = json.loads(r["context"]) if r["context"] else {}
        facts = "\n".join(f"  {k}：{v}" for k, v in ctx.get("facts", []))
        qs = "\n".join(f"  {i}. {q}" for i, q in enumerate(ctx.get("questions", []), 1))

        console.print(Panel(
            f"[bold]{r['code']} {r.get('name', '')}[/bold]\n{r['title']}\n\n"
            f"[cyan]事实（系统整理）[/cyan]\n{facts}\n\n"
            f"[yellow]待你判断[/yellow]\n{qs}\n\n"
            f"[dim]时限 {r['due_at']} · 状态 {r['status']}[/dim]",
            title=f"[{r['severity']}] 复核任务"))
        console.print("[dim]注意：本界面刻意不显示持仓成本与浮盈亏，"
                      "以切断沉没成本对判断的干扰[/dim]")


@events_app.command("notify")
def notify(
    dry_run: Annotated[bool, typer.Option(help="只打印不发送")] = True,
) -> None:
    """推送未通知的任务到飞书。

    默认 dry-run —— 推送是外发动作，需显式 --no-dry-run 才真正发送。
    """
    with open_db(DB_PATH) as db:
        pending = db.query(
            "SELECT t.*, b.name FROM review_task t "
            "LEFT JOIN stock_basic b ON b.code = t.code "
            "WHERE t.status = 'pending' ORDER BY t.severity")
        if pending.empty:
            console.print("[green]无待推送任务[/green]")
            raise typer.Exit()

        notifier = FeishuNotifier()
        sent = 0
        for _, r in pending.iterrows():
            ctx = json.loads(r["context"]) if r["context"] else {}
            ch = channel_for(r["severity"])

            if dry_run:
                console.print(f"[dim]将推送到 {ch}：[{r['severity']}] "
                              f"{r['code']} {r['title']}[/dim]")
                continue

            ok, detail = notifier.send_task_card(
                ch, severity=r["severity"], title=r["title"],
                code=r["code"], name=str(r.get("name") or ""),
                facts=[tuple(f) for f in ctx.get("facts", [])],
                questions=ctx.get("questions", []),
                task_id=r["task_id"], due=str(r["due_at"])[:16],
            )
            db.conn.execute(
                "INSERT INTO notification_log "
                "(notif_id, task_id, channel, success, error) VALUES (?,?,?,?,?)",
                [r["task_id"][:16], r["task_id"], f"feishu_{ch}", ok,
                 None if ok else detail])
            if ok:
                db.conn.execute(
                    "UPDATE review_task SET status='notified', "
                    "notified_at=current_timestamp WHERE task_id=?", [r["task_id"]])
                sent += 1
            else:
                console.print(f"[red]推送失败 {r['code']}: {detail}[/red]")

        if dry_run:
            console.print(f"\n[yellow]dry-run：共 {len(pending)} 条待推送。"
                          f"加 --no-dry-run 实际发送[/yellow]")
        else:
            console.print(f"[green]已推送 {sent}/{len(pending)}[/green]")


@events_app.command("review")
def review(
    task_id: str,
    verdict: Annotated[str, typer.Option(
        help="sentiment(情绪面) / fundamental(价值面) / insufficient(信息不足)")],
    would_rebuy: Annotated[bool, typer.Option(
        "--rebuy/--no-rebuy", help="以今天的价格和信息是否还会买入")],
    reasoning: Annotated[str, typer.Option(help="判断依据")],
    next_review: Annotated[str, typer.Option(
        help="下次复核日期 YYYY-MM-DD，verdict=insufficient 时必填")] = "",
    action: Annotated[str, typer.Option(help="none/add/reduce/exit")] = "none",
    falsified: Annotated[str, typer.Option(help="被证伪的假设条目")] = "",
) -> None:
    """提交复核结论。

    verdict=insufficient 必须给出下次复核时点 —— 不允许无限期挂起，
    那是逃避面对亏损标的最常见的形式。
    """
    if verdict not in {"sentiment", "fundamental", "insufficient"}:
        console.print("[red]verdict 须为 sentiment / fundamental / insufficient[/red]")
        raise typer.Exit(1)
    if verdict == "insufficient" and not next_review:
        console.print("[red]信息不足时必须指定 --next-review，不允许无限期挂起[/red]")
        raise typer.Exit(1)

    with open_db(DB_PATH) as db:
        exists = db.query("SELECT code FROM review_task WHERE task_id = ?", [task_id])
        if exists.empty:
            console.print("[red]任务不存在[/red]")
            raise typer.Exit(1)

        db.conn.execute(
            "INSERT OR REPLACE INTO review_conclusion "
            "(task_id, code, verdict, would_rebuy, reasoning, falsified_items, "
            " next_review_date, action_taken) VALUES (?,?,?,?,?,?,?,?)",
            [task_id, exists["code"].iloc[0], verdict, would_rebuy, reasoning,
             falsified or None,
             dt.date.fromisoformat(next_review) if next_review else None, action])
        db.conn.execute(
            "UPDATE review_task SET status='concluded', "
            "concluded_at=current_timestamp WHERE task_id=?", [task_id])

    console.print(f"[green]已记录结论：{verdict}[/green]")
    if verdict == "sentiment" and not would_rebuy:
        console.print("[yellow]提示：判定为情绪面（假设未动摇），"
                      "却回答不会重新买入——这两者存在矛盾，值得再想一次[/yellow]")


@events_app.command("watch")
def watch(
    code: str,
    tier: Annotated[str, typer.Option(help="watch(观察池) / holding(持仓)")] = "watch",
    thesis: Annotated[str, typer.Option(help="买入理由")] = "",
    assumptions: Annotated[str, typer.Option(help="核心假设")] = "",
    falsification: Annotated[str, typer.Option(help="证伪条件，用；分隔")] = "",
    target: Annotated[float, typer.Option(help="目标价")] = 0.0,
) -> None:
    """加入观察池或持仓。

    证伪条件是整套方法论的枢纽：写不出证伪条件，
    说明你并不真的理解这家公司，也就不应该买。
    它同时是 L6 监控的直接输入。
    """
    if tier == "holding" and not falsification:
        console.print("[yellow]警告：持仓未填写证伪条件。"
                      "L6 监控将无从核对，复核任务也会缺少判断依据。[/yellow]")

    with open_db(DB_PATH) as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO watchlist "
            "(code, added_at, tier, thesis, core_assumptions, falsification, target_price) "
            "VALUES (?,?,?,?,?,?,?)",
            [code, dt.date.today(), tier, thesis or None, assumptions or None,
             falsification or None, target or None])
    console.print(f"[green]{code} 已加入 {tier}[/green]")
