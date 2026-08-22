"""决策记录与纪律检查（L5 决策层的人机接口）。

**本模块不告诉你该买什么，只做两件机械的事：**

1. 决策前把你自己定的约束摆在眼前（`csg check`）
2. 决策后把「你做了什么、当时违反了什么、你是否明知故犯」记下来（`csg decide`）

为什么这两件事就是纪律的全部机制：

规则早就写在 config/position.yaml 里，检查代码早就在 decision/constraints.py，
但此前二者只在你主动去问时说话，且无视之后毫无痕迹。于是
「我定了单票 15% 上限」与「我持有 26.8% 的亿纬锂能」可以长期共存，
不产生任何摩擦。**摩擦是纪律的来源，而摩擦需要被记录才存在。**

单次破戒不重要——这是你的钱，系统也没有下单权限。
重要的是复盘时能算出「破戒 N 次、其中 M 次亏钱」。
那个数字会改变行为，而贴在墙上的规则不会。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json

import typer
from rich.console import Console
from rich.table import Table

from csg.decision import constraints as C
from csg.storage import open_db

DB_PATH = "data/csg.duckdb"
console = Console()

decide_app = typer.Typer(help="决策记录与纪律检查")


def _holdings(db) -> tuple[list[C.Holding], float, dict]:
    """当前持仓、总市值、每只的最新价。"""
    pos = db.query("""
        SELECT p.code, p.shares, p.cost_price, b.name,
               i.industry_name AS industry
        FROM position p
        LEFT JOIN stock_basic b ON b.code = p.code
        LEFT JOIN industry_member i ON i.code = p.code
                                   AND i.taxonomy = 'em_industry'
    """)
    if pos.empty:
        return [], 0.0, {}

    px = {}
    for c in pos["code"]:
        r = db.query("SELECT close FROM daily_quote WHERE code = ? "
                     "ORDER BY trade_date DESC LIMIT 1", [c])
        px[c] = float(r["close"].iloc[0]) if not r.empty else 0.0

    total = sum(float(r["shares"]) * px[r["code"]] for _, r in pos.iterrows())
    hs = [
        C.Holding(code=r["code"], name=r["name"] or "",
                  weight=(float(r["shares"]) * px[r["code"]] / total) if total else 0.0,
                  industry=r["industry"] or "")
        for _, r in pos.iterrows()
    ]
    return hs, total, px


@decide_app.command("check")
def check(cash: float = 0.0) -> None:
    """当前组合对照全部约束 —— 决策前先看这个。

    `cash` 为可用现金（元），用于计算现金比例约束。
    """
    with open_db(DB_PATH) as db:
        hs, total, _ = _holdings(db)
        if not hs:
            console.print("[yellow]无持仓记录。用 csg watch add 或直接写 position 表[/yellow]")
            return
        cash_ratio = cash / (total + cash) if (total + cash) else 0.0

        t = Table(title=f"组合健康（市值 {total / 1e4:.2f} 万，现金比例 {cash_ratio:.1%}）")
        for c in ("项目", "当前", "约束", "状态"):
            t.add_column(c)
        for _, r in C.portfolio_health(hs, cash_ratio).iterrows():
            t.add_row(str(r["项目"]), str(r["当前"]), str(r["约束"]), str(r["状态"]))
        console.print(t)

        # 逐只查单票上限：portfolio_health 只报最大的那只，
        # 但同时超限的可能有多只，每一只都要点名
        cfg = C.load_config()
        limit = cfg["single_position"]["max_weight"]
        over = [h for h in hs if h.weight > limit]
        if over:
            console.print(f"\n[red]单票超限 {len(over)} 只（上限 {limit:.0%}）[/red]")
            for h in sorted(over, key=lambda x: -x.weight):
                excess = h.weight - limit
                console.print(f"  {h.code} {h.name}  {h.weight:.1%} "
                              f"（超 {excess:.1%}，需减约 {excess * total / 1e4:.2f} 万）")

        # 回撤：减仓决策最需要的数字，而 portfolio_health 不含它。
        #
        # 同时给「距成本」与「距峰值」——两者差别巨大且含义不同：
        #   距成本  你亏了多少，决定心理压力
        #   距峰值  这只票跌了多少，决定它是否已进入价值区
        # 亿纬锂能距成本仅 −7.1%，距峰值 −60.7%。
        # 只看前者会以为没事，只看后者会以为该抄底。
        dd = Table(title="回撤（后复权，等同净值）")
        for c in ("代码", "名称", "权重", "距成本", "距峰值", "峰值日"):
            dd.add_column(c)
        hard = cfg["exit"]["hard_stop_drawdown"]
        for h in sorted(hs, key=lambda x: -x.weight):
            q = db.query("""
                WITH v AS (SELECT trade_date d, close px, close * adj_factor hv
                           FROM daily_quote WHERE code = ?)
                SELECT (SELECT px FROM v ORDER BY d DESC LIMIT 1) last_px,
                       (SELECT hv FROM v ORDER BY d DESC LIMIT 1) last_hv,
                       (SELECT max(hv) FROM v) peak,
                       (SELECT d FROM v WHERE hv = (SELECT max(hv) FROM v) LIMIT 1) pd
            """, [h.code])
            cp = db.query("SELECT cost_price FROM position WHERE code = ?", [h.code])
            if q.empty or cp.empty:
                continue
            last = float(q["last_px"].iloc[0])
            from_cost = last / float(cp["cost_price"].iloc[0]) - 1
            from_peak = float(q["last_hv"].iloc[0]) / float(q["peak"].iloc[0]) - 1
            mark = " ⛔" if from_peak <= hard else ""
            dd.add_row(h.code, h.name, f"{h.weight:.1%}",
                       f"{from_cost:+.1%}", f"{from_peak:+.1%}",
                       str(q["pd"].iloc[0])[:10] + mark)
        console.print(dd)
        console.print(f"[dim]⛔ = 距峰值超过兜底线 {hard:.0%}。"
                      f"⚠️ position.yaml 未写明该线的基准是成本还是峰值——"
                      f"此处按峰值判定，若你的本意是成本，请改配置并留 commit[/dim]")

        for v in C.stalled_reviews(db):
            console.print(f"\n[yellow]⚠ [{v.rule}] {v.message}[/yellow]")

        # 无视记录：让历史破戒在每次检查时都露一次脸
        od = db.query("""
            SELECT count(*) n, sum(CASE WHEN ret_20 < 0 THEN 1 ELSE 0 END) lose
            FROM decision_log WHERE overridden
        """)
        n = int(od["n"].iloc[0] or 0)
        if n:
            lose = int(od["lose"].iloc[0] or 0)
            console.print(f"\n[red]历史破戒 {n} 次[/red]"
                          f"（其中 20 日后亏损 {lose} 次）"
                          f"  详见 csg decide log --overridden")


@decide_app.command("log")
def show_log(
    code: str = "",
    overridden: bool = False,
    limit: int = 30,
) -> None:
    """决策历史。`--overridden` 只看破戒记录。"""
    where, params = [], []
    if code:
        where.append("code = ?")
        params.append(code)
    if overridden:
        where.append("overridden")
    sql = "SELECT * FROM decision_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY decided_at DESC LIMIT ?"
    params.append(limit)

    with open_db(DB_PATH) as db:
        df = db.query(sql, params)
    if df.empty:
        console.print("[dim]无记录。每次买卖后用 csg decide record 记一笔[/dim]")
        return

    for _, r in df.iterrows():
        tag = "[red]破戒[/red] " if r["overridden"] else ""
        console.print(f"\n{tag}[bold]{str(r['decided_at'])[:16]} "
                      f"{r['action']} {r['code']}[/bold]  "
                      f"{r['shares'] or ''}股 @{r['price'] or ''}  "
                      f"理由类别 {r['reason']}")
        console.print(f"  {r['rationale']}")
        if r["weight_before"] is not None and r["weight_after"] is not None:
            console.print(f"  权重 {r['weight_before']:.1%} → {r['weight_after']:.1%}")
        if r["violations"]:
            for v in json.loads(r["violations"]):
                console.print(f"  [red]违反[/red] {v}")
        if r["ret_20"] is not None:
            console.print(f"  20 日后 {r['ret_20']:+.2%}")


@decide_app.command("record")
def record(
    code: str,
    action: str = typer.Option(..., help="buy / add / reduce / exit"),
    shares: int = typer.Option(..., help="股数（正数）"),
    price: float = typer.Option(..., help="成交价"),
    reason: str = typer.Option(..., help="理由类别，见 position.yaml exit.reasons"),
    rationale: str = typer.Option(..., help="文字理由，必填"),
) -> None:
    """记一笔决定 —— **在你下单之后立刻记，别等复盘时回忆。**

    命令会自动带上决策当时的约束违规清单。若存在违规，
    会要求你确认「明知故犯」，并把该确认存进 overridden 列。

    `reason` 里 `emotional` 是最有价值的一类。诚实填它，
    复盘统计才有意义；填成别的，你只是骗自己而已，
    而这套系统存在的全部理由就是盯着你别自欺。
    """
    if action not in {"buy", "add", "reduce", "exit"}:
        console.print("[red]action 须为 buy / add / reduce / exit[/red]")
        raise typer.Exit(1)

    cfg = C.load_config()
    allowed = set(cfg["exit"]["reasons"]) | {"new_position", "rebalance"}
    if reason not in allowed:
        console.print(f"[red]reason 须为其一：{', '.join(sorted(allowed))}[/red]")
        raise typer.Exit(1)

    with open_db(DB_PATH) as db:
        hs, total, _px = _holdings(db)
        cur = next((h for h in hs if h.code == code), None)
        w_before = cur.weight if cur else 0.0

        delta = shares * price * (1 if action in {"buy", "add"} else -1)
        new_total = total + delta
        cur_mv = w_before * total
        w_after = (cur_mv + delta) / new_total if new_total > 0 else 0.0

        vios: list[str] = []
        if action in {"buy", "add"}:
            ind = db.query("SELECT industry_name FROM industry_member "
                           "WHERE code = ? AND taxonomy = 'em_industry'", [code])
            for v in C.check_new_position(
                    hs, code, w_after,
                    industry=(ind["industry_name"].iloc[0] if not ind.empty else "")):
                vios.append(f"[{v.rule}] {v.message}")
            if action == "add":
                for v in C.check_add_position_for(db, code, w_after - w_before):
                    vios.append(f"[{v.rule}] {v.message}")

        if vios:
            console.print(f"\n[red]该操作违反 {len(vios)} 条约束：[/red]")
            for v in vios:
                console.print(f"  {v}")
            console.print("\n[yellow]系统无下单权限，拦不住你。"
                          "但这次无视会被记录，并计入破戒统计。[/yellow]")
            if not typer.confirm("确认明知故犯并记录？"):
                console.print("[dim]未记录。若你已下单，请务必回来补记——"
                              "漏记的破戒让统计失去意义[/dim]")
                raise typer.Exit(1)

        did = hashlib.md5(
            f"{code}|{action}|{dt.datetime.now().isoformat()}".encode()).hexdigest()[:16]
        db.conn.execute(
            """INSERT INTO decision_log
               (decision_id, code, action, shares, price, weight_before,
                weight_after, reason, rationale, violations, overridden)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [did, code, action, shares, price, w_before, w_after,
             reason, rationale,
             json.dumps(vios, ensure_ascii=False) if vios else None,
             bool(vios)])

    console.print(f"\n[green]已记录[/green] {action} {code} "
                  f"权重 {w_before:.1%} → {w_after:.1%}"
                  f"{'  [red](破戒)[/red]' if vios else ''}")


@decide_app.command("review")
def review_outcomes() -> None:
    """回填每笔决定之后 20 / 60 日的表现 —— 破戒统计的数据来源。

    只回填窗口已走完的决定。未走完的留空，不用当前价凑数：
    那会让最近的决定整体呈现出行情最近的涨跌。
    """
    with open_db(DB_PATH) as db:
        rows = db.query("SELECT decision_id, code, decided_at FROM decision_log "
                        "WHERE ret_20 IS NULL OR ret_60 IS NULL")
        if rows.empty:
            console.print("[dim]无待回填记录[/dim]")
            return
        n = 0
        for _, r in rows.iterrows():
            d0 = str(r["decided_at"])[:10]
            for h, col in ((20, "ret_20"), (60, "ret_60")):
                q = db.query("""
                    WITH p AS (
                        SELECT trade_date, close * adj_factor AS v,
                               row_number() OVER (ORDER BY trade_date) AS rn
                        FROM daily_quote WHERE code = ? AND trade_date > ?
                    )
                    SELECT (SELECT v FROM p WHERE rn = 1) a,
                           (SELECT v FROM p WHERE rn = ? + 1) b
                """, [r["code"], d0, h])
                a, b = q["a"].iloc[0], q["b"].iloc[0]
                if a and b:
                    db.conn.execute(
                        f"UPDATE decision_log SET {col} = ? WHERE decision_id = ?",
                        [float(b) / float(a) - 1, r["decision_id"]])
                    n += 1
        console.print(f"[green]回填 {n} 个窗口[/green]")
