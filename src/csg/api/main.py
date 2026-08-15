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


# ----------------------------------------------------------------------
# 研报检索
# ----------------------------------------------------------------------

# 评级到分值，用于判定调整方向。
# 绝对评级几乎无信息量（实测买入占 94%），有价值的是**变化**——
# 尤其下调：逆着分析师的激励机制，不愿得罪覆盖对象却仍下调。
_RATING_SCORE_SQL = """
    CASE rating
        WHEN '买入' THEN 5 WHEN '增持' THEN 4
        WHEN '持有' THEN 3 WHEN '中性' THEN 3
        WHEN '减持' THEN 2 WHEN '卖出' THEN 1
    END
"""


class ReportQuery(BaseModel):
    """研报检索结果的单条记录。"""

    report_id: str
    title: str
    publish_date: str
    institution: str
    code: str
    stock_name: str | None
    industry: str | None
    rating: str | None
    prev_rating: str | None
    rating_change: str | None      # up / down / unchanged / first
    has_forecast: bool
    pdf_url: str | None


@app.get("/api/institutions")
def list_institutions(
    min_reports: Annotated[int, Query(ge=1)] = 1,
    keyword: str | None = None,
) -> list[dict]:
    """机构列表及其研报量、覆盖股票数、时间跨度。

    覆盖股票数与时间跨度用于判断该机构的统计量是否可信——
    只覆盖三只股票的机构，其「准确率」没有解读价值。
    """
    with read_db() as db:
        sql = """
            SELECT institution AS name,
                   count(*)                    AS report_count,
                   count(DISTINCT code)        AS stock_count,
                   min(publish_date)           AS first_date,
                   max(publish_date)           AS last_date
            FROM research_report
            WHERE institution IS NOT NULL AND institution <> ''
        """
        params: list = []
        if keyword:
            sql += " AND institution ILIKE ?"
            params.append(f"%{keyword}%")
        sql += " GROUP BY institution HAVING count(*) >= ? ORDER BY report_count DESC"
        params.append(min_reports)
        return _records(db.query(sql, params))


@app.get("/api/report-industries")
def list_report_industries() -> list[dict]:
    """研报涉及的行业列表。"""
    with read_db() as db:
        return _records(db.query("""
            SELECT industry AS name, count(*) AS report_count
            FROM research_report
            WHERE industry IS NOT NULL AND industry <> ''
            GROUP BY industry ORDER BY report_count DESC
        """))


@app.get("/api/reports")
def search_reports(
    start: str | None = None,
    end: str | None = None,
    title: Annotated[str | None, Query(description="标题模糊匹配")] = None,
    institution: str | None = None,
    code: str | None = None,
    stock: Annotated[str | None, Query(description="股票代码或名称模糊匹配")] = None,
    industry: str | None = None,
    rating: str | None = None,
    rating_change: Annotated[
        str | None,
        Query(pattern="^(up|down|unchanged|first)$",
              description="评级调整方向。down 最具信息量")
    ] = None,
    has_forecast: bool | None = None,
    order_by: Annotated[str, Query(pattern="^(publish_date|institution|code)$")] = "publish_date",
    desc: bool = True,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict:
    """研报检索。

    `report_id` 由自然键哈希生成（md5(code+日期+机构+标题)），
    稳定且无需改动表结构——已入库数据不必迁移。

    **rating_change 是本接口最有价值的过滤条件**：绝对评级筛选意义有限
    （买入占 94%），而「谁在什么时候改了主意」才携带信息。
    """
    conds: list[str] = []
    params: list = []

    if start:
        conds.append("r.publish_date >= ?")
        params.append(dt.date.fromisoformat(start))
    if end:
        conds.append("r.publish_date <= ?")
        params.append(dt.date.fromisoformat(end))
    if title:
        conds.append("r.title ILIKE ?")
        params.append(f"%{title}%")
    if institution:
        conds.append("r.institution ILIKE ?")
        params.append(f"%{institution}%")
    if code:
        conds.append("r.code = ?")
        params.append(code)
    if stock:
        conds.append("(r.code ILIKE ? OR b.name ILIKE ?)")
        params += [f"%{stock}%", f"%{stock}%"]
    if industry:
        conds.append("r.industry = ?")
        params.append(industry)
    if rating:
        conds.append("r.rating = ?")
        params.append(rating)

    where = f"WHERE {' AND '.join(conds)}" if conds else ""

    # 评级变化需在窗口函数计算后过滤，故用外层 CTE
    change_filter = ""
    if rating_change:
        change_filter = "WHERE rating_change = ?"

    base = f"""
        WITH scored AS (
            SELECT r.*, b.name AS stock_name,
                   {_RATING_SCORE_SQL} AS score
            FROM research_report r
            LEFT JOIN stock_basic b ON b.code = r.code
            {where}
        ),
        seq AS (
            SELECT *,
                   lag(score)  OVER w AS prev_score,
                   lag(rating) OVER w AS prev_rating
            FROM scored
            WINDOW w AS (PARTITION BY code, institution ORDER BY publish_date)
        ),
        labeled AS (
            SELECT *,
                   CASE
                       WHEN prev_score IS NULL THEN 'first'
                       WHEN score > prev_score THEN 'up'
                       WHEN score < prev_score THEN 'down'
                       ELSE 'unchanged'
                   END AS rating_change
            FROM seq
        )
        SELECT * FROM labeled {change_filter}
    """
    if rating_change:
        params.append(rating_change)

    with read_db() as db:
        total = db.query(
            f"SELECT count(*) AS n FROM ({base})", list(params))["n"].iloc[0]

        direction = "DESC" if desc else "ASC"
        rows = db.query(
            f"""
            SELECT md5(l.code || CAST(l.publish_date AS VARCHAR)
                       || l.institution || l.title)      AS report_id,
                   l.title, l.publish_date, l.institution,
                   l.code, l.stock_name, l.industry,
                   l.rating, l.prev_rating, l.rating_change, l.pdf_url,
                   EXISTS (
                       SELECT 1 FROM research_forecast f
                       WHERE f.code = l.code
                         AND f.publish_date = l.publish_date
                         AND f.institution = l.institution
                   ) AS has_forecast
            FROM ({base}) l
            ORDER BY l.{order_by} {direction}, l.code
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, (page - 1) * page_size],
        )

        if has_forecast is not None and not rows.empty:
            rows = rows[rows["has_forecast"] == has_forecast]

        return {
            "total": int(total),
            "page": page,
            "page_size": page_size,
            "items": _records(rows),
        }


@app.get("/api/reports/{report_id}")
def report_detail(report_id: str) -> dict:
    """单条研报详情，含其盈利预测（若有）。"""
    with read_db() as db:
        df = db.query(
            """
            SELECT r.*, b.name AS stock_name,
                   md5(r.code || CAST(r.publish_date AS VARCHAR)
                       || r.institution || r.title) AS report_id
            FROM research_report r
            LEFT JOIN stock_basic b ON b.code = r.code
            WHERE md5(r.code || CAST(r.publish_date AS VARCHAR)
                      || r.institution || r.title) = ?
            """, [report_id])
        if df.empty:
            raise HTTPException(404, "研报不存在")
        row = _records(df)[0]

        fc = db.query(
            "SELECT forecast_year, eps, pe, snapshot_date FROM research_forecast "
            "WHERE code = ? AND publish_date = ? AND institution = ? "
            "ORDER BY forecast_year, snapshot_date",
            [row["code"], row["publish_date"], row["institution"]])
        row["forecasts"] = _records(fc)
        return row


# ----------------------------------------------------------------------
# 研报分析（聚合接口）
# ----------------------------------------------------------------------

@app.get("/api/stocks/{code}/overview")
def stock_overview(code: str) -> dict:
    """公司概览：基本信息 + 最新估值 + 最新财务快照。

    估值取自 daily_basic（由 baostock K 线接口附带的 peTTM/pbMRQ/psTTM 填充）。
    该表为空时返回 null 而非 0——**缺失与零是两回事**，
    前端应显示「—」，不能显示 0 让人误读为「市盈率为零」。
    """
    with read_db() as db:
        basic = db.query(
            "SELECT b.code, b.name, b.market, b.exchange, b.list_date, "
            "       b.delist_date, b.is_active, i.industry_name "
            "FROM stock_basic b "
            "LEFT JOIN industry_member i ON i.code = b.code "
            "  AND i.taxonomy = 'em_industry' WHERE b.code = ?", [code])
        if basic.empty:
            raise HTTPException(404, f"未找到股票 {code}")

        quote = db.query(
            "SELECT trade_date, close, pct_chg, turnover, volume, amount "
            "FROM daily_quote WHERE code = ? ORDER BY trade_date DESC LIMIT 1", [code])
        valuation = db.query(
            "SELECT trade_date, pe_ttm, pb, ps_ttm, total_mv, circ_mv "
            "FROM daily_basic WHERE code = ? ORDER BY trade_date DESC LIMIT 1", [code])

        # 52 周区间：券商 App 的常规展示项，用于判断当前价位在年内的位置
        band = db.query(
            "SELECT max(high) AS high_52w, min(low) AS low_52w "
            "FROM daily_quote WHERE code = ? "
            "  AND trade_date >= current_date - 365", [code])

        return {
            "basic": _records(basic)[0],
            "quote": _records(quote)[0] if not quote.empty else None,
            "valuation": _records(valuation)[0] if not valuation.empty else None,
            "band": _records(band)[0] if not band.empty else None,
        }


@app.get("/api/reports/{report_id}/analysis")
def report_analysis(
    report_id: str,
    window_days: Annotated[int, Query(ge=30, le=1500)] = 500,
) -> dict:
    """研报分析上下文，一次请求返回页面所需的全部数据。

    **两条设计要点：**

    1. 财务数据取**研报发布时点已披露**的（PIT），而非最新。
       这样才能看清分析师当时手里有什么，进而判断其结论是否站得住。
       用最新财务去评价一份两年前的研报，是拿他不可能知道的信息苛责他。

    2. `implied_price` 为**推算值**：预测EPS × 发布日PE。
       ⚠️ 不是研报目标价——接口不提供目标价，实测其「预测PE」
       等于发布日股价÷预测EPS，故乘积仅能还原发布日股价本身。
       此推算的含义是「若估值倍数维持不变，业绩兑现后对应的价格」，
       前端必须标注为推算，不得呈现为研报观点。
    """
    with read_db() as db:
        rep = db.query(
            """
            SELECT r.*, b.name AS stock_name,
                   md5(r.code || CAST(r.publish_date AS VARCHAR)
                       || r.institution || r.title) AS report_id
            FROM research_report r
            LEFT JOIN stock_basic b ON b.code = r.code
            WHERE md5(r.code || CAST(r.publish_date AS VARCHAR)
                      || r.institution || r.title) = ?
            """, [report_id])
        if rep.empty:
            raise HTTPException(404, "研报不存在")
        report = _records(rep)[0]
        code = report["code"]
        pub = dt.date.fromisoformat(str(report["publish_date"])[:10])

        # 同机构上一次评级 —— 变化才有信息量
        prev = db.query(
            "SELECT rating, publish_date FROM research_report "
            "WHERE code = ? AND institution = ? AND publish_date < ? "
            "ORDER BY publish_date DESC LIMIT 1",
            [code, report["institution"], pub])
        report["prev_rating"] = prev["rating"].iloc[0] if not prev.empty else None

        # 行情窗口：发布日前后各取一段，便于观察「发布前走势」与「发布后表现」
        quotes = db.query(
            """
            SELECT trade_date AS time,
                   open * adj_factor  AS open,
                   high * adj_factor  AS high,
                   low  * adj_factor  AS low,
                   close * adj_factor AS close,
                   volume, amount, pct_chg
            FROM daily_quote
            WHERE code = ? AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            [code, pub - dt.timedelta(days=window_days),
             pub + dt.timedelta(days=window_days)])

        # 发布后表现：入场价取发布日之后首个交易日收盘
        perf = db.query(
            """
            WITH px AS (
                SELECT trade_date, close * adj_factor AS p,
                       high * adj_factor AS hi, low * adj_factor AS lo,
                       row_number() OVER (ORDER BY trade_date) AS rn
                FROM daily_quote WHERE code = ? AND trade_date > ?
            ),
            e AS (SELECT p AS entry FROM px WHERE rn = 1)
            SELECT (SELECT entry FROM e)                        AS entry_price,
                   max(CASE WHEN rn = 21  THEN p END) / (SELECT entry FROM e) - 1 AS ret_20,
                   max(CASE WHEN rn = 61  THEN p END) / (SELECT entry FROM e) - 1 AS ret_60,
                   max(CASE WHEN rn = 121 THEN p END) / (SELECT entry FROM e) - 1 AS ret_120,
                   max(CASE WHEN rn = 251 THEN p END) / (SELECT entry FROM e) - 1 AS ret_250,
                   max(CASE WHEN rn <= 251 THEN hi END) / (SELECT entry FROM e) - 1 AS max_gain,
                   min(CASE WHEN rn <= 251 THEN lo END) / (SELECT entry FROM e) - 1 AS max_loss
            FROM px WHERE rn <= 251
            """, [code, pub])

        # 发布时点的估值（用于把预测 EPS 换算成隐含价格）
        val_at_pub = db.query(
            "SELECT trade_date, pe_ttm, pb, total_mv FROM daily_basic "
            "WHERE code = ? AND trade_date <= ? ORDER BY trade_date DESC LIMIT 1",
            [code, pub])
        px_at_pub = db.query(
            "SELECT close FROM daily_quote WHERE code = ? AND trade_date <= ? "
            "ORDER BY trade_date DESC LIMIT 1", [code, pub])

        forecasts = _records(db.query(
            "SELECT forecast_year, eps, pe, snapshot_date FROM research_forecast "
            "WHERE code = ? AND publish_date = ? AND institution = ? "
            "ORDER BY forecast_year", [code, pub, report["institution"]]))

        # 隐含价格 = 预测EPS × 发布日PE。见本函数 docstring 第 2 条。
        pe_ref = (float(val_at_pub["pe_ttm"].iloc[0])
                  if not val_at_pub.empty and val_at_pub["pe_ttm"].iloc[0] else None)
        for f in forecasts:
            f["implied_price"] = (
                round(f["eps"] * pe_ref, 2)
                if pe_ref and f.get("eps") else None)
            f["basis"] = "预测EPS × 发布日PE（推算，非研报目标价）"

        # 财务：严格取发布时点已披露者
        panel = metrics.load_pit_panel(db, pub, codes=[code], periods=8)
        fin_periods: list = []
        if not panel.empty:
            rated = flags.evaluate(metrics.compute_ratios(panel))
            keep = [c for c in rated.columns if not c.startswith("_") and c != "month"]
            fin_periods = _records(rated[keep].tail(4))

        return {
            "report": report,
            "forecasts": forecasts,
            "quotes": _records(quotes),
            "performance": _records(perf)[0] if not perf.empty else None,
            "valuation_at_publish": (
                _records(val_at_pub)[0] if not val_at_pub.empty else None),
            "price_at_publish": (
                float(px_at_pub["close"].iloc[0]) if not px_at_pub.empty else None),
            "financials_pit": fin_periods,
            "pit_note": "财务数据为研报发布时点已披露者，非最新",
        }
