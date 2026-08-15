"""FastAPI 后端。

**唯一职责：把 csg 已有的能力暴露成 HTTP，不重新实现任何金融逻辑。**

PIT 查询、累计值还原、TTM、红旗规则全部复用 csg 模块。
`disclosure_date` 过滤这类要害若在前端或此处重写一遍，
等于埋下第二套 bug——而这类 bug 是静默的，不会报错，
只会让数字悄悄变得好看。

DuckDB 并发限制：单进程写锁。读接口一律用只读连接，
写接口（提交复核结论）临时开写连接；采集任务运行期间写会失败，
此时返回明确提示，而非静默吞掉。
"""

from __future__ import annotations

import datetime as dt
import json
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from csg import universe
from csg.analysis import flags, metrics
from csg.storage import Database

DB_PATH = "data/csg.duckdb"

app = FastAPI(title="CSG API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@contextmanager
def read_db():
    """只读连接。

    ⚠️ DuckDB 的写锁是**进程级排他**的：采集任务运行期间，
    连只读连接都无法建立。这不是可以绕过的实现细节，是选型代价。
    此时返回 503 并说明原因，让前端显示「数据更新中」，
    而不是抛出一个看不懂的 IOException。
    """
    try:
        db = Database(DB_PATH, read_only=True)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"数据更新中，数据库暂被采集任务锁定：{type(exc).__name__}",
        ) from exc
    try:
        yield db
    finally:
        db.close()


@contextmanager
def write_db():
    """写连接。与采集任务互斥，冲突时给出可操作的提示。"""
    try:
        db = Database(DB_PATH, read_only=False)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"数据库被占用（采集任务运行中？）：{exc}",
        ) from exc
    try:
        yield db
    finally:
        db.close()


def _records(df) -> list[dict[str, Any]]:
    """DataFrame → JSON 安全的记录列表。NaN 转 None，日期转 ISO 字符串。"""
    if df is None or df.empty:
        return []
    out = df.where(df.notna(), None).to_dict(orient="records")
    for row in out:
        for k, v in row.items():
            if isinstance(v, (dt.date, dt.datetime)):
                row[k] = v.isoformat()
            elif hasattr(v, "item"):
                row[k] = v.item()
    return out


# ----------------------------------------------------------------------
# 概览
# ----------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    """数据健康状况。

    静默失效是本系统最危险的故障——看起来在跑，实际早已停止。
    前端应把陈旧水位显著标出。
    """
    with read_db() as db:
        counts = db.query("""
            SELECT 'stock_basic' AS name, count(*) AS rows FROM stock_basic
            UNION ALL SELECT 'daily_quote', count(*) FROM daily_quote
            UNION ALL SELECT 'research_report', count(*) FROM research_report
            UNION ALL SELECT 'fin_income', count(*) FROM fin_income
            UNION ALL SELECT 'event', count(*) FROM event
            UNION ALL SELECT 'review_task', count(*) FROM review_task
        """)
        watermarks = db.query("""
            SELECT dataset,
                   count(*) AS scopes,
                   sum(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                   max(last_success_date) AS newest
            FROM sync_watermark GROUP BY dataset ORDER BY dataset
        """)
        return {"counts": _records(counts), "watermarks": _records(watermarks)}


@app.get("/api/pool")
def get_pool() -> dict:
    with read_db() as db:
        pool = universe.resolve_universe(db)
        by_theme = (pool.groupby(["theme", "industry_name"])
                    .size().reset_index(name="count"))
        return {"total": len(pool), "breakdown": _records(by_theme),
                "stocks": _records(pool)}


# ----------------------------------------------------------------------
# 个股
# ----------------------------------------------------------------------

@app.get("/api/stocks/{code}")
def stock_detail(code: str) -> dict:
    with read_db() as db:
        basic = db.query(
            "SELECT b.*, i.industry_name FROM stock_basic b "
            "LEFT JOIN industry_member i ON i.code = b.code "
            "AND i.taxonomy='em_industry' WHERE b.code = ?", [code])
        if basic.empty:
            raise HTTPException(404, f"未找到股票 {code}")

        watch = db.query("SELECT * FROM watchlist WHERE code = ?", [code])
        return {
            "basic": _records(basic)[0],
            "watchlist": _records(watch)[0] if not watch.empty else None,
        }


@app.get("/api/stocks/{code}/quotes")
def stock_quotes(
    code: str,
    start: str = "2016-01-01",
    end: str | None = None,
    adjust: Annotated[str, Query(pattern="^(hfq|none)$")] = "hfq",
) -> list[dict]:
    """K 线数据。

    后复权用于计算与技术分析——它不会因未来的除权事件改变历史值。
    前复权仅可用于展示，且必须实时计算，绝不落盘。
    """
    end_date = dt.date.fromisoformat(end) if end else dt.date.today()
    with read_db() as db:
        px = "close * adj_factor" if adjust == "hfq" else "close"
        df = db.query(
            f"""
            SELECT trade_date AS time,
                   open  * {'adj_factor' if adjust == 'hfq' else '1'} AS open,
                   high  * {'adj_factor' if adjust == 'hfq' else '1'} AS high,
                   low   * {'adj_factor' if adjust == 'hfq' else '1'} AS low,
                   {px} AS close,
                   volume, amount, pct_chg
            FROM daily_quote
            WHERE code = ? AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            [code, dt.date.fromisoformat(start), end_date])
        return _records(df)


@app.get("/api/stocks/{code}/financials")
def stock_financials(code: str, as_of: str | None = None) -> dict:
    """财务面板。

    严格 point-in-time：只返回 `as_of` 时点**已披露**的财报。
    默认 as_of 为今天。
    """
    ref = dt.date.fromisoformat(as_of) if as_of else dt.date.today()
    with read_db() as db:
        panel = metrics.load_pit_panel(db, ref, codes=[code])
        if panel.empty:
            return {"as_of": ref.isoformat(), "periods": []}
        rated = flags.evaluate(metrics.compute_ratios(panel))
        keep = [c for c in rated.columns
                if not c.startswith("_") and c != "month"]
        return {"as_of": ref.isoformat(), "periods": _records(rated[keep])}


@app.get("/api/stocks/{code}/reports")
def stock_reports(code: str, limit: int = 100) -> list[dict]:
    """研报列表。

    评级本身信息量极低（实测买入占 94%），前端不应突出显示评级，
    重点应放在**评级变化**上。
    """
    with read_db() as db:
        df = db.query(
            """
            SELECT publish_date, institution, title, rating, pdf_url,
                   lag(rating) OVER (PARTITION BY institution ORDER BY publish_date)
                       AS prev_rating
            FROM research_report WHERE code = ?
            ORDER BY publish_date DESC LIMIT ?
            """, [code, limit])
        return _records(df)


# ----------------------------------------------------------------------
# 事件与复核任务
# ----------------------------------------------------------------------

@app.get("/api/events")
def list_events(limit: int = 100, event_type: str | None = None) -> list[dict]:
    with read_db() as db:
        sql = ("SELECT e.*, b.name FROM event e "
               "LEFT JOIN stock_basic b ON b.code = e.code")
        params: list = []
        if event_type:
            sql += " WHERE e.event_type = ?"
            params.append(event_type)
        sql += " ORDER BY e.ref_date DESC, e.detected_at DESC LIMIT ?"
        params.append(limit)
        return _records(db.query(sql, params))


@app.get("/api/tasks")
def list_tasks(status: str = "pending") -> list[dict]:
    with read_db() as db:
        sql = ("SELECT t.*, b.name, "
               "(t.due_at < current_timestamp AND t.status <> 'concluded') AS overdue "
               "FROM review_task t LEFT JOIN stock_basic b ON b.code = t.code")
        params: list = []
        if status != "all":
            sql += " WHERE t.status = ?"
            params.append(status)
        sql += " ORDER BY t.severity, t.due_at"
        rows = _records(db.query(sql, params))
        for r in rows:
            if r.get("context"):
                r["context"] = json.loads(r["context"])
        return rows


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str) -> dict:
    with read_db() as db:
        df = db.query(
            "SELECT t.*, b.name FROM review_task t "
            "LEFT JOIN stock_basic b ON b.code = t.code WHERE t.task_id = ?",
            [task_id])
        if df.empty:
            raise HTTPException(404, "任务不存在")
        row = _records(df)[0]
        if row.get("context"):
            row["context"] = json.loads(row["context"])

        wl = db.query("SELECT * FROM watchlist WHERE code = ?", [row["code"]])
        row["watchlist"] = _records(wl)[0] if not wl.empty else None
        return row


class ConclusionIn(BaseModel):
    """复核结论。

    would_rebuy 为必填：「以今天的价格与信息，我会重新买入吗？」
    这道题切断沉没成本，把「亏了要不要割」重构为
    「这是不是我今天愿意持有的资产」。
    """

    verdict: str = Field(pattern="^(sentiment|fundamental|insufficient)$")
    would_rebuy: bool
    reasoning: str = Field(min_length=1)
    falsified_items: str | None = None
    next_review_date: str | None = None
    action_taken: str = Field(default="none", pattern="^(none|add|reduce|exit)$")


@app.post("/api/tasks/{task_id}/conclude")
def conclude_task(task_id: str, body: ConclusionIn) -> dict:
    # 「信息不足」必须给出下次复核时点——不允许无限期挂起，
    # 那是逃避面对亏损标的最常见的形式。
    if body.verdict == "insufficient" and not body.next_review_date:
        raise HTTPException(400, "verdict=insufficient 时必须指定 next_review_date")

    with write_db() as db:
        exists = db.query("SELECT code FROM review_task WHERE task_id = ?", [task_id])
        if exists.empty:
            raise HTTPException(404, "任务不存在")

        db.conn.execute(
            "INSERT OR REPLACE INTO review_conclusion "
            "(task_id, code, verdict, would_rebuy, reasoning, falsified_items, "
            " next_review_date, action_taken) VALUES (?,?,?,?,?,?,?,?)",
            [task_id, exists["code"].iloc[0], body.verdict, body.would_rebuy,
             body.reasoning, body.falsified_items,
             dt.date.fromisoformat(body.next_review_date)
             if body.next_review_date else None,
             body.action_taken])
        db.conn.execute(
            "UPDATE review_task SET status='concluded', "
            "concluded_at=current_timestamp WHERE task_id=?", [task_id])

    warning = None
    if body.verdict == "sentiment" and not body.would_rebuy:
        warning = ("判定为情绪面（假设未动摇）却不愿重新买入，"
                   "这两者存在矛盾，值得再想一次")
    return {"ok": True, "warning": warning}


# ----------------------------------------------------------------------
# 验证结果
# ----------------------------------------------------------------------

@app.get("/api/validations")
def list_validations(vtype: str | None = None) -> list[dict]:
    from csg.validation import store

    with read_db() as db:
        return _records(store.list_runs(db, vtype))


@app.get("/api/validations/{run_id}")
def validation_detail(run_id: str) -> dict:
    from csg.validation import store

    with read_db() as db:
        meta = db.query(
            "SELECT * FROM validation_run WHERE run_id = ?", [run_id])
        if meta.empty:
            raise HTTPException(404, "run_id 不存在")
        row = _records(meta)[0]
        for key in ("params", "data_snapshot"):
            if row.get(key):
                row[key] = json.loads(row[key])
        row["results"] = {
            k: _records(v) for k, v in store.load_run(db, run_id).items()
        }
        return row


@app.get("/api/watchlist")
def get_watchlist() -> list[dict]:
    with read_db() as db:
        return _records(db.query(
            "SELECT w.*, b.name FROM watchlist w "
            "LEFT JOIN stock_basic b ON b.code = w.code ORDER BY w.tier, w.code"))
