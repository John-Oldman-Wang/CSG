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
import math
from contextlib import contextmanager
from typing import Annotated, Any

import pandas as pd
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
    """DataFrame → JSON 安全的记录列表。NaN 转 None，日期转 ISO 字符串。

    ⚠️ `df.where(df.notna(), None)` 对 **float64 列不起作用**：
    pandas 会把 None 强制回 NaN，列仍是 float64。于是 NaN 一路走到
    `to_dict()`，被序列化成字面量 `NaN`——那不是合法 JSON，
    `JSON.parse` 直接抛错，而前端只会表现为「这个接口坏了」。

    因此逐值判定，不依赖 DataFrame 层的替换。
    """
    if df is None or df.empty:
        return []
    out = df.to_dict(orient="records")
    for row in out:
        for k, v in row.items():
            if v is None or (isinstance(v, float) and math.isnan(v)):
                row[k] = None
            elif isinstance(v, (dt.date, dt.datetime)):
                row[k] = v.isoformat()
            elif pd.isna(v):                      # NaT、pd.NA
                row[k] = None
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
    adjust: Annotated[str, Query(pattern="^(qfq|hfq|none)$")] = "qfq",
) -> list[dict]:
    """K 线数据。

    三种口径的**用途互不重叠，用错即为错误数据**：

    - `qfq` 前复权（默认）：**唯一可用于展示**的口径。
      最新一日等于真实成交价，与券商 App 显示一致。
      实时计算、绝不落盘——它会因未来的除权事件改变历史值。
    - `hfq` 后复权：用于计算收益率与技术指标，历史值恒定不变。
      **其绝对值以上市首日为基准，没有现实含义，不可当价格显示。**
      例：300568 于 2026-08-14 真实开盘 14.51，后复权为 106.56（因子 7.34）。
    - `none` 原始价：当日真实成交价，但跨除权日会出现断崖，不适合看走势。

    前复权与后复权的**收益率完全相同**（比值相同，归一化常数抵消），
    因此把展示口径从 hfq 改为 qfq 不影响任何已有的收益计算。
    """
    end_date = dt.date.fromisoformat(end) if end else dt.date.today()
    with read_db() as db:
        if adjust == "none":
            mul = "1"
        elif adjust == "hfq":
            mul = "adj_factor"
        else:
            # 前复权：以**全序列最新一日**的因子归一化——不是请求区间的末日。
            # 若按区间末日归一，缩放时间轴会导致纵轴数值整体变化
            # （查 2016 年得到 31.18，查至今得到 4.25），与券商 App 不符。
            # 基准恒定，图形缩放时纵轴才稳定。
            mul = ("adj_factor / (SELECT adj_factor FROM daily_quote "
                   "WHERE code = ? AND adj_factor IS NOT NULL "
                   "ORDER BY trade_date DESC LIMIT 1)")

        params: list = []
        for _ in range(4 if adjust == "qfq" else 0):
            params.append(code)

        df = db.query(
            f"""
            SELECT trade_date AS time,
                   open  * ({mul}) AS open,
                   high  * ({mul}) AS high,
                   low   * ({mul}) AS low,
                   close * ({mul}) AS close,
                   volume, amount, pct_chg
            FROM daily_quote
            WHERE code = ? AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            [*params, code, dt.date.fromisoformat(start), end_date])
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

        rows = _attach_returns(db, rows)

        return {
            "total": int(total),
            "page": page,
            "page_size": page_size,
            "horizons": RETURN_HORIZONS,
            "items": _records(rows),
        }


# ======================================================================
# 研报发布后的表现
# ======================================================================

RETURN_HORIZONS = (20, 50, 100)

# 月度基准缓存：键为 (horizon, 行情末日)。
# 行情末日变化即失效，故不会取到陈旧基准。
_BENCH_CACHE: dict[tuple[int, str], dict] = {}


def _monthly_benchmark(db, horizon: int, end_iso: str) -> dict:
    """同月全样本研报收益的中位数 —— 超额收益的基准。

    **绝对收益在这里没有意义**：牛市里所有研报都"赢"，那不是研报的功劳。
    与 /api/institution-winrates 用同一口径，两处数字才对得上——
    若表格显示绝对收益、徽章显示超额胜率，同一份研报会同时呈现
    "涨了 30%" 与 "跑输"，读者无从判断哪个是真的。
    """
    key = (horizon, end_iso)
    if key in _BENCH_CACHE:
        return _BENCH_CACHE[key]

    df = db.query(
        """
        WITH px AS (
            SELECT code, trade_date, close * adj_factor AS p,
                   row_number() OVER (PARTITION BY code ORDER BY trade_date) AS rn
            FROM daily_quote
            WHERE close IS NOT NULL AND adj_factor IS NOT NULL AND close > 0
        ),
        maxrn AS (SELECT code, max(rn) AS last_rn FROM px GROUP BY code),
        ent AS (
            SELECT r.code, r.publish_date, min(px.rn) AS entry_rn
            FROM research_report r
            JOIN px ON px.code = r.code AND px.trade_date > r.publish_date
            GROUP BY r.code, r.publish_date
        )
        SELECT date_trunc('month', e.publish_date) AS m,
               median((h.p / en.p) - 1) AS med
        FROM ent e
        JOIN maxrn mx ON mx.code = e.code
        JOIN px en ON en.code = e.code AND en.rn = e.entry_rn
        JOIN px h  ON h.code  = e.code AND h.rn  = e.entry_rn + ?
        WHERE e.entry_rn + ? <= mx.last_rn
        GROUP BY 1
        """, [horizon, horizon])

    bench = {str(pd.Timestamp(r["m"]).date())[:7]: float(r["med"])
             for _, r in df.iterrows()}
    _BENCH_CACHE[key] = bench
    return bench


def _attach_returns(db, rows: pd.DataFrame) -> pd.DataFrame:
    """为当前页的研报补上 20/50/100 日超额收益。

    只对本页的 N 行计算，不做全表扫描——研报库近两万条，
    每次翻页全算会让接口无法使用（CLAUDE.md 第 5 条：聚合下推 SQL，
    pandas 只处理结果集）。

    **窗口未走完的研报返回 None，不返回 0，也不用当前价凑数。**
    发布日 + horizon 个交易日若超出数据末日，该研报尚无结果；
    填任何数字都会让"最近发布的研报"整体呈现出行情最近的涨跌，
    那是纯粹的时点偏差。
    """
    for h in RETURN_HORIZONS:
        rows[f"ret_{h}"] = None
    rows["elapsed_days"] = None
    if rows.empty:
        return rows

    end_iso = str(pd.Timestamp(
        db.query("SELECT max(trade_date) AS d FROM daily_quote")["d"].iloc[0]).date())

    pairs = rows[["code", "publish_date"]].drop_duplicates()
    db.conn.register("_page_keys", pairs)
    try:
        marks = ",".join("?" * len(RETURN_HORIZONS))
        got = db.query(
            f"""
            WITH px AS (
                SELECT code, trade_date, close * adj_factor AS p,
                       row_number() OVER (PARTITION BY code ORDER BY trade_date) AS rn
                FROM daily_quote
                WHERE code IN (SELECT code FROM _page_keys)
                  AND close IS NOT NULL AND adj_factor IS NOT NULL AND close > 0
            ),
            maxrn AS (SELECT code, max(rn) AS last_rn FROM px GROUP BY code),
            ent AS (
                SELECT k.code, k.publish_date, min(px.rn) AS entry_rn
                FROM _page_keys k
                JOIN px ON px.code = k.code AND px.trade_date > k.publish_date
                GROUP BY k.code, k.publish_date
            ),
            hz(h) AS (SELECT unnest([{marks}]))
            SELECT e.code, e.publish_date, hz.h,
                   (h.p / en.p) - 1 AS ret,
                   -- 自入场日起已走完的交易日数。窗口未满时前端据此
                   -- 显示「12/20 日」而非留空——空白无法区分
                   -- 「还没到时候」与「数据缺了」。
                   mx.last_rn - e.entry_rn AS elapsed
            FROM ent e
            CROSS JOIN hz
            JOIN maxrn mx ON mx.code = e.code
            JOIN px en ON en.code = e.code AND en.rn = e.entry_rn
            LEFT JOIN px h ON h.code = e.code AND h.rn = e.entry_rn + hz.h
            """, list(RETURN_HORIZONS))
    finally:
        db.conn.unregister("_page_keys")

    if got.empty:
        return rows

    benches = {h: _monthly_benchmark(db, h, end_iso) for h in RETURN_HORIZONS}
    idx: dict = {}
    elapsed: dict = {}
    for _, r in got.iterrows():
        key2 = (r["code"], pd.Timestamp(r["publish_date"]).date())
        elapsed[key2] = int(r["elapsed"])
        if pd.isna(r["ret"]):
            continue                      # 窗口未走完，由 elapsed 表达
        med = benches[int(r["h"])].get(str(key2[1])[:7])
        if med is None:
            continue                      # 该月无基准，超额无从计算
        idx[(*key2, int(r["h"]))] = float(r["ret"]) - med

    keys = [(c, pd.Timestamp(d).date())
            for c, d in zip(rows["code"], rows["publish_date"], strict=True)]
    for h in RETURN_HORIZONS:
        rows[f"ret_{h}"] = [idx.get((*k, h)) for k in keys]
    rows["elapsed_days"] = [elapsed.get(k) for k in keys]
    return rows


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
        #
        # ⚠️ 复权基准取**发布日**，不是今天，也不能用后复权原值。
        #    本页要把研报的目标价/预测 EPS 隐含价标注在 K 线上，
        #    而那些价格是**发布当时的实际价格**。三种口径的后果：
        #      后复权    K 线被抬到以上市首日为基准的数值（300568 达 7.34 倍），
        #                目标价贴上去差一个数量级，标注完全错位
        #      前复权到今天  同样错位，只是倍数不同
        #      前复权到发布日  发布日当天 = 真实价，与目标价同坐标系，
        #                且窗口内不会出现除权断崖 ✓
        quotes = db.query(
            """
            WITH base AS (
                SELECT adj_factor AS f FROM daily_quote
                WHERE code = ? AND trade_date <= ? AND adj_factor IS NOT NULL
                ORDER BY trade_date DESC LIMIT 1
            )
            SELECT trade_date AS time,
                   open  * adj_factor / (SELECT f FROM base) AS open,
                   high  * adj_factor / (SELECT f FROM base) AS high,
                   low   * adj_factor / (SELECT f FROM base) AS low,
                   close * adj_factor / (SELECT f FROM base) AS close,
                   volume, amount, pct_chg
            FROM daily_quote
            WHERE code = ? AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            [code, pub,
             code, pub - dt.timedelta(days=window_days),
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


# ----------------------------------------------------------------------
# 机构研报胜率统计
# ----------------------------------------------------------------------

@app.get("/api/institution-stats")
def institution_stats(
    horizon: Annotated[int, Query(ge=20, le=250, description="考察窗口，交易日")] = 250,
    group_by: Annotated[str, Query(pattern="^(year|half|quarter|all)$")] = "year",
    min_samples: Annotated[int, Query(ge=5)] = 10,
    institution: str | None = None,
) -> dict:
    """机构研报的胜率与超额收益，按时间分组。

    **两条必须理解的设计：**

    1. **排除未走完窗口的研报**。发布日 + horizon 个交易日若超出数据末日，
       该研报尚无结果，纳入统计会系统性高估或低估——取决于最近这段
       行情涨跌，属于典型的幸存者式偏差。

    2. **胜率以超额收益为准，不是绝对收益**。牛市里所有研报都"赢"，
       那不是研报的功劳。超额 = 个股收益 − 同月全样本中位数。

    ⚠️ 解读警告：已验证机构排名的跨期秩相关为 −0.286（比随机更差），
    故本表**不可用作未来的机构权重**。它的用途是查看历史分布，
    而非挑选"最准的机构"。
    """
    period_expr = {
        "year": "CAST(year(o.publish_date) AS VARCHAR)",
        "half": "CAST(year(o.publish_date) AS VARCHAR) || 'H' || "
                "CAST(CASE WHEN month(o.publish_date) <= 6 THEN 1 ELSE 2 END AS VARCHAR)",
        "quarter": "CAST(year(o.publish_date) AS VARCHAR) || 'Q' || "
                   "CAST(quarter(o.publish_date) AS VARCHAR)",
        "all": "'全期'",
    }[group_by]

    inst_filter = "AND r.institution = ?" if institution else ""
    params: list = [horizon, horizon]
    if institution:
        params.append(institution)
    params += [min_samples]

    with read_db() as db:
        sql = f"""
        WITH px AS (
            SELECT code, trade_date, close * adj_factor AS p,
                   row_number() OVER (PARTITION BY code ORDER BY trade_date) AS rn
            FROM daily_quote
            WHERE close IS NOT NULL AND adj_factor IS NOT NULL AND close > 0
        ),
        maxrn AS (SELECT code, max(rn) AS last_rn FROM px GROUP BY code),
        ent AS (
            SELECT r.code, r.publish_date, r.institution, r.rating,
                   min(px.rn) AS entry_rn
            FROM research_report r
            JOIN px ON px.code = r.code AND px.trade_date > r.publish_date
            WHERE r.rating IS NOT NULL AND r.rating <> '' {inst_filter}
            GROUP BY r.code, r.publish_date, r.institution, r.rating
        ),
        outcome AS (
            SELECT e.code, e.publish_date, e.institution, e.rating,
                   (h.p / en.p) - 1 AS ret
            FROM ent e
            JOIN maxrn m ON m.code = e.code
            JOIN px en ON en.code = e.code AND en.rn = e.entry_rn
            JOIN px h  ON h.code  = e.code AND h.rn  = e.entry_rn + ?
            -- 只保留窗口已走完的：入场点 + horizon 不得超出该股数据末端
            WHERE e.entry_rn + ? <= m.last_rn
        ),
        benched AS (
            SELECT o.*,
                   o.ret - median(o.ret) OVER (
                       PARTITION BY date_trunc('month', o.publish_date)
                   ) AS excess
            FROM outcome o
        )
        SELECT {period_expr.replace('o.', '')} AS 期间,
               institution AS 机构,
               count(*) AS 样本数,
               round(median(excess), 4) AS 超额中位,
               round(avg(CASE WHEN excess > 0 THEN 1.0 ELSE 0.0 END), 3) AS 胜率,
               round(median(ret), 4) AS 绝对收益中位,
               round(min(excess), 4) AS 最差,
               round(max(excess), 4) AS 最好
        FROM benched o
        GROUP BY 1, 2
        HAVING count(*) >= ?
        ORDER BY 1 DESC, 超额中位 DESC
        """
        rows = db.query(sql, params)

        coverage = db.query(
            """
            SELECT count(*) AS 研报总数,
                   CAST(max(publish_date) AS VARCHAR) AS 最新研报,
                   CAST(max(trade_date) AS VARCHAR) AS 行情末日
            FROM research_report, (SELECT max(trade_date) AS trade_date FROM daily_quote)
            """)

        return {
            "horizon": horizon,
            "group_by": group_by,
            "coverage": _records(coverage)[0] if not coverage.empty else None,
            "rows": _records(rows),
            "caveat": (
                "胜率以超额收益（个股收益 − 同月全样本中位数）为准；"
                "已排除窗口未走完的研报。"
                "⚠️ 机构排名的跨期秩相关实测为 −0.286（比随机更差），"
                "本表仅供查看历史分布，不可用作未来的机构权重。"
            ),
        }


# ----------------------------------------------------------------------
# 估值筛选
# ----------------------------------------------------------------------

@app.get("/api/valuation-screen")
def valuation_screen(
    theme: Annotated[str | None, Query(description="new_energy / ai_compute")] = None,
    max_pe: Annotated[float, Query(description="PE 上限")] = 40.0,
    min_pe: Annotated[float, Query(description="PE 下限，滤掉亏损与异常")] = 5.0,
    max_pe_pct: Annotated[float, Query(ge=0, le=1, description="PE 历史分位上限")] = 0.5,
    min_roe: Annotated[float, Query(description="ROE 下限")] = 0.08,
    min_cfo_ni: Annotated[float, Query(description="经营现金流/净利润下限")] = 0.5,
    max_flag_score: Annotated[int, Query(description="红旗分值上限")] = 3,
    require_growth: Annotated[bool, Query(description="要求净利润同比为正")] = True,
    limit: int = 50,
) -> dict:
    """估值筛选：找「相对自身历史偏低」且基本面未恶化的标的。

    **不是简单的低 PE 排序。** 低 PE 在周期行业里常是陷阱——
    盈利顶部时 PE 最低，而那正是盈利即将下行的时点
    （天齐锂业 2022 年净利 241 亿、PE 极低，随后跌至亏损 79 亿）。

    故采用四层交叉：

    1. **PE 历史分位** —— 与自身过去比，而非与其他公司比。
       不同商业模式的合理 PE 天然不同，跨公司比 PE 意义有限。
    2. **盈利质量** —— ROE 与经营现金流/净利润。
       低 PE + 现金流差 = 利润可能是账面数字。
    3. **红旗过滤** —— 排除财务异常者。
       便宜往往有便宜的理由。
    4. **增长要求** —— 净利润同比为正，避免买在盈利崩塌的途中。

    ⚠️ 本接口输出的是**候选名单**，不是买入建议。
    它只回答「哪些标的值得看一眼」，不回答「哪些该买」。
    """
    with read_db() as db:
        pool_codes: list[str] | None = None
        if theme:
            pool = universe.resolve_universe(db)
            pool_codes = pool.loc[pool["theme"] == theme, "code"].tolist()
            if not pool_codes:
                return {"total": 0, "items": [], "note": f"主题 {theme} 无标的"}

        today = dt.date.today()
        panel = metrics.load_pit_panel(db, today, codes=pool_codes, periods=12)
        if panel.empty:
            return {"total": 0, "items": [], "note": "无财务数据"}

        snap = metrics.latest_snapshot(
            flags.evaluate(metrics.compute_ratios(panel)))

        # PE 历史分位：与自身过去 3 年比较
        codes = snap["code"].tolist()
        ph = ", ".join("?" * len(codes))
        val = db.query(
            f"""
            WITH recent AS (
                SELECT code, trade_date, pe_ttm, pb
                FROM daily_basic
                WHERE code IN ({ph}) AND pe_ttm IS NOT NULL AND pe_ttm > 0
                  AND trade_date >= current_date - 1095
            ),
            latest AS (
                SELECT code, pe_ttm, pb,
                       row_number() OVER (PARTITION BY code ORDER BY trade_date DESC) rn
                FROM recent
            )
            SELECT r.code,
                   max(CASE WHEN l.rn = 1 THEN l.pe_ttm END) AS pe_ttm,
                   max(CASE WHEN l.rn = 1 THEN l.pb END)     AS pb,
                   -- 当前 PE 在自身近三年分布中的位置
                   avg(CASE WHEN r.pe_ttm <=
                        (SELECT pe_ttm FROM latest x WHERE x.code = r.code AND x.rn = 1)
                       THEN 1.0 ELSE 0.0 END) AS pe_percentile,
                   count(*) AS obs
            FROM recent r JOIN latest l ON l.code = r.code
            GROUP BY r.code HAVING count(*) >= 250
            """, codes)

        merged = snap.merge(val, on="code", how="inner")
        names = db.query(
            f"SELECT b.code, b.name, i.industry_name FROM stock_basic b "
            f"LEFT JOIN industry_member i ON i.code=b.code AND i.taxonomy='em_industry' "
            f"WHERE b.code IN ({ph})", codes)
        merged = merged.merge(names, on="code", how="left")

        m = merged
        cond = (
            (m["pe_ttm"].between(min_pe, max_pe))
            & (m["pe_percentile"] <= max_pe_pct)
            & (pd.to_numeric(m["roe_ttm"], errors="coerce") >= min_roe)
            & (pd.to_numeric(m["cfo_to_ni"], errors="coerce") >= min_cfo_ni)
            & (m["flag_score"] <= max_flag_score)
        )
        if require_growth:
            cond &= pd.to_numeric(m["profit_yoy"], errors="coerce") > 0

        out = m[cond.fillna(False)].copy()
        out = out.sort_values("pe_percentile").head(limit)

        cols = ["code", "name", "industry_name", "report_period", "pe_ttm", "pb",
                "pe_percentile", "roe_ttm", "gross_margin_ttm", "cfo_to_ni",
                "revenue_yoy", "profit_yoy", "debt_ratio", "flag_score", "flag_names"]
        return {
            "total": len(out),
            "screened_from": len(merged),
            "items": _records(out[[c for c in cols if c in out.columns]]),
            "note": (
                "PE 分位为与自身近三年比较（非跨公司比较）。"
                "⚠️ 低 PE 在周期行业常是陷阱：盈利顶部时 PE 最低，"
                "而那正是盈利即将下行之时。本表是候选名单，不是买入建议。"
            ),
        }


# ----------------------------------------------------------------------
# 股票池分级（机构投研流程的核心制度）
# ----------------------------------------------------------------------

@app.get("/api/pool-tiers")
def pool_tiers() -> dict:
    """四层股票池的构成与流转。

    **这是机构投研流程中最值得复制的一条制度**：
    标的必须逐级晋升，每级有明确的入池条件与产出要求，
    不允许「看好就买」直接跳级。

        L0 全市场    仅轻量数据，用于行业分位与市场水位，不做个股分析
        L1 基础池    定期筛选的范围（能力圈内行业）
        L2 观察池    已研究认可、写下证伪条件、等价格
        L3 持仓      最高强度跟踪

    L1→L2 的门槛是**写出证伪条件**——写不出说明还没真正想清楚，
    此时正确的动作是继续研究而非买入。这条门槛是整个流程的关键。
    """
    with read_db() as db:
        l1 = universe.resolve_universe(db)
        l2 = db.query("""
            SELECT w.code, b.name, i.industry_name AS industry, w.tier,
                   CAST(w.added_at AS VARCHAR) AS added_at,
                   w.thesis, w.core_assumptions, w.falsification, w.target_price
            FROM watchlist w
            LEFT JOIN stock_basic b ON b.code = w.code
            LEFT JOIN industry_member i ON i.code = w.code
              AND i.taxonomy = 'em_industry'
            ORDER BY w.tier DESC, w.code
        """)
        l3 = db.query("""
            SELECT p.code, b.name, i.industry_name AS industry,
                   p.shares, p.cost_price,
                   (SELECT close FROM daily_quote q WHERE q.code = p.code
                    ORDER BY trade_date DESC LIMIT 1) AS price
            FROM position p
            LEFT JOIN stock_basic b ON b.code = p.code
            LEFT JOIN industry_member i ON i.code = p.code
              AND i.taxonomy = 'em_industry'
        """)
        l0_count = db.query("SELECT count(*) AS n FROM stock_basic")["n"].iloc[0]

        # 持仓的市值与权重在查询时计算，避免落盘后过期
        holdings: list[dict] = []
        if not l3.empty:
            l3["market_value"] = l3["shares"] * l3["price"]
            total = l3["market_value"].sum()
            l3["weight"] = l3["market_value"] / total if total else 0.0
            l3["pnl_pct"] = l3["price"] / l3["cost_price"] - 1
            l3["cost_value"] = l3["shares"] * l3["cost_price"]
            holdings = _records(l3.sort_values("weight", ascending=False))

        # 各层之间的归属关系：观察池中有多少已建仓、基础池中有多少已研究
        held = set(l3["code"]) if not l3.empty else set()
        watched = set(l2["code"]) if not l2.empty else set()

        watchlist_rows = _records(l2)
        for r in watchlist_rows:
            r["in_position"] = r["code"] in held
            # 证伪条件数量——L1→L2 晋升的实质门槛
            r["falsification_count"] = len(
                [c for c in str(r.get("falsification") or "").split("；") if c.strip()])

        return {
            "tiers": [
                {"tier": "L0", "name": "全市场", "count": int(l0_count),
                 "desc": "仅轻量数据，用于行业分位与市场水位，不做个股分析"},
                {"tier": "L1", "name": "基础池", "count": len(l1),
                 "desc": "能力圈内行业，定期筛选的范围"},
                {"tier": "L2", "name": "观察池", "count": len(l2),
                 "desc": "已研究认可、写下证伪条件、等价格"},
                {"tier": "L3", "name": "持仓", "count": len(l3),
                 "desc": "最高强度跟踪"},
            ],
            "l1_breakdown": _records(
                l1.groupby(["theme", "industry_name"]).size()
                  .reset_index(name="count")) if not l1.empty else [],
            "watchlist": watchlist_rows,
            "holdings": holdings,
            "promotion_gate": (
                "L1→L2 的门槛是写出证伪条件。写不出说明还没真正想清楚，"
                "此时正确的动作是继续研究而非买入。"
            ),
            "coverage": {
                "l1_researched": len(watched),
                "l2_held": len(watched & held),
                "held_not_watched": sorted(held - watched),
            },
        }


@app.get("/api/portfolio")
def portfolio() -> dict:
    """持仓组合与约束检查。

    **输出是「是否违反你已定的规则」，不是「建议买多少」。**

    系统没有下单权限，故违规提示可被无视——但每次无视都会留下记录，
    供复盘时统计破戒次数。机构的制度优势正在于此：
    把「不能做什么」写进系统，情绪上头时有东西拦住。
    """
    from csg.decision.constraints import load_config

    with read_db() as db:
        df = db.query("""
            SELECT p.code, b.name, i.industry_name AS industry,
                   p.shares, p.cost_price,
                   (SELECT close FROM daily_quote q WHERE q.code = p.code
                    ORDER BY trade_date DESC LIMIT 1) AS price
            FROM position p
            LEFT JOIN stock_basic b ON b.code = p.code
            LEFT JOIN industry_member i ON i.code = p.code
              AND i.taxonomy = 'em_industry'
        """)
        if df.empty:
            return {"holdings": [], "violations": [], "summary": None}

        df["market_value"] = df["shares"] * df["price"]
        df["cost_value"] = df["shares"] * df["cost_price"]
        total_mv = float(df["market_value"].sum())
        total_cost = float(df["cost_value"].sum())
        df["weight"] = df["market_value"] / total_mv
        df["pnl"] = df["market_value"] - df["cost_value"]
        df["pnl_pct"] = df["price"] / df["cost_price"] - 1

        themes = universe.target_industries()
        ind_to_theme = {n: t for t, names in themes.items() for n in names}
        df["theme"] = df["industry"].map(ind_to_theme).fillna("other")

        cfg = load_config()
        s, c, p = cfg["single_position"], cfg["concentration"], cfg["portfolio"]
        violations: list[dict] = []

        for _, r in df.iterrows():
            if r["weight"] > s["max_weight"]:
                violations.append({
                    "rule": "单票上限", "severity": "block",
                    "target": f"{r['code']} {r['name']}",
                    "current": round(float(r["weight"]), 4),
                    "limit": s["max_weight"],
                    "message": f"{r['name']} 权重 {r['weight']:.1%}，"
                               f"超出上限 {s['max_weight']:.0%}"})

        for key, limit, label in [
            ("industry", c["max_industry_weight"], "单行业上限"),
            ("theme", c["max_theme_weight"], "单主题上限"),
        ]:
            grouped = df.groupby(key)["weight"].sum()
            for name, w in grouped.items():
                if w > limit:
                    violations.append({
                        "rule": label, "severity": "block", "target": str(name),
                        "current": round(float(w), 4), "limit": limit,
                        "message": f"{name} 合计 {w:.1%}，超出上限 {limit:.0%}"})

        return {
            "holdings": _records(df.sort_values("weight", ascending=False)),
            "violations": violations,
            "summary": {
                "total_market_value": round(total_mv, 2),
                "total_cost": round(total_cost, 2),
                "pnl": round(total_mv - total_cost, 2),
                "pnl_pct": round(total_mv / total_cost - 1, 4),
                "count": len(df),
                "count_limit": f"{p['min_count']}–{p['max_count']}",
                "industry_breakdown": _records(
                    df.groupby("industry")["weight"].sum()
                      .reset_index(name="weight").sort_values("weight", ascending=False)),
                "theme_breakdown": _records(
                    df.groupby("theme")["weight"].sum()
                      .reset_index(name="weight").sort_values("weight", ascending=False)),
            },
        }


@app.get("/api/institution-pnl")
def institution_pnl(
    horizon: int = 20,
    capital: float = 10000,
    min_samples: int = 50,
) -> dict:
    """按机构统计「每份研报投入固定金额、持有 N 个交易日」的累计盈亏。

    比胜率直观——盈亏是钱，胜率是抽象数。但有两个坑必须避开：

    **① 基准必须用均值，不能用中位数。**
    收益分布右偏（股票能涨 200%，最多跌 100%）。以中位数为基准时，
    半数研报低于零，但少数大赢家把**均值**拉高，于是几乎每家机构的
    平均超额都为正——实测 18 家里 17 家两期皆盈。那不是 alpha，是偏度。
    均值基准使全样本超额严格为零和（实测 −0.000000%），
    机构间才是真正的相对比较。

    **② 必须分期呈现，不能只给全期合计。**
    只看全期排名会直接引导出「跟着第一名买」，而实测秩相关为 −0.366、
    前 1/3 留存率 0.333（恰等于随机基准）——历史排名不可外推。
    分期并列时，名次交叉本身就是答案。

    `capital` 只是把收益率换算成金钱的标尺，不代表可执行的策略：
    某家机构五年发 1374 份研报，每份一万意味着投入 1374 万。
    """
    with read_db() as db:
        rows = db.query(
            """
            WITH px AS (
                SELECT code, trade_date, close * adj_factor AS p,
                       row_number() OVER (PARTITION BY code ORDER BY trade_date) AS rn
                FROM daily_quote
                WHERE close IS NOT NULL AND adj_factor IS NOT NULL AND close > 0
            ),
            mx AS (SELECT code, max(rn) AS last_rn FROM px GROUP BY code),
            ent AS (
                SELECT r.code, r.publish_date, r.institution, min(px.rn) AS ern
                FROM research_report r
                JOIN px ON px.code = r.code AND px.trade_date > r.publish_date
                WHERE r.rating IS NOT NULL AND r.rating <> ''
                GROUP BY 1, 2, 3
            ),
            oc AS (
                SELECT e.institution, e.publish_date, (h.p / en.p) - 1 AS ret
                FROM ent e
                JOIN mx ON mx.code = e.code
                JOIN px en ON en.code = e.code AND en.rn = e.ern
                JOIN px h  ON h.code  = e.code AND h.rn  = e.ern + ?
                -- 窗口未走完的研报尚无结果，纳入即为时点偏差
                WHERE e.ern + ? <= mx.last_rn
            ),
            b AS (
                SELECT *,
                       -- 均值基准：使超额严格零和，见 docstring ①
                       ret - avg(ret) OVER (
                           PARTITION BY date_trunc('month', publish_date)
                       ) AS exc
                FROM oc
            )
            SELECT institution AS 机构,
                   CASE WHEN year(publish_date) BETWEEN 2018 AND 2022 THEN '发现期'
                        WHEN year(publish_date) >= 2023 THEN '验证期' END AS 期,
                   count(*) AS 份数,
                   avg(exc) * count(*) * ? AS 累计超额,
                   avg(ret) * count(*) * ? AS 累计绝对,
                   avg(CASE WHEN exc > 0 THEN 1.0 ELSE 0.0 END) AS 胜率
            FROM b
            WHERE year(publish_date) >= 2018
            GROUP BY 1, 2
            """,
            [horizon, horizon, capital, capital])

    if rows.empty:
        return {"horizon": horizon, "capital": capital, "rows": [], "stability": {}}

    wide = rows.pivot(index="机构", columns="期")
    for col in ("份数", "累计超额", "胜率"):
        for per in ("发现期", "验证期"):
            if (col, per) not in wide.columns:
                wide[(col, per)] = None

    keep = (wide[("份数", "发现期")].fillna(0) >= min_samples) & (
        wide[("份数", "验证期")].fillna(0) >= min_samples)
    wide = wide[keep]
    if wide.empty:
        return {"horizon": horizon, "capital": capital, "rows": [], "stability": {}}

    out = pd.DataFrame({
        "机构": wide.index,
        "发现份数": wide[("份数", "发现期")].astype(int).to_numpy(),
        "发现累计": wide[("累计超额", "发现期")].round(0).to_numpy(),
        "发现胜率": wide[("胜率", "发现期")].to_numpy(),
        "验证份数": wide[("份数", "验证期")].astype(int).to_numpy(),
        "验证累计": wide[("累计超额", "验证期")].round(0).to_numpy(),
        "验证胜率": wide[("胜率", "验证期")].to_numpy(),
    })
    out["发现排名"] = out["发现累计"].rank(ascending=False).astype(int)
    out["验证排名"] = out["验证累计"].rank(ascending=False).astype(int)
    out["名次变化"] = out["发现排名"] - out["验证排名"]

    n = len(out)
    k = max(1, n // 3)
    top = set(out.nsmallest(k, "发现排名")["机构"])
    still = len(top & set(out.nsmallest(k, "验证排名")["机构"]))
    # 秩相关 = 排名的 Pearson 相关（手工实现，避免引入 scipy）
    rho = float(out["发现排名"].corr(out["验证排名"]))

    return {
        "horizon": horizon,
        "capital": capital,
        "min_samples": min_samples,
        "rows": _records(out.sort_values("发现排名")),
        "stability": {
            "机构数": n,
            "秩相关": round(rho, 3),
            "前1/3留存": round(still / k, 3),
            "随机基准": round(k / n, 3),
            "两期皆盈": int(((out["发现累计"] > 0) & (out["验证累计"] > 0)).sum()),
        },
    }


# ======================================================================
# 「跟着研报买」的入场规则
# ======================================================================
#
# 研报发布**次日开盘价**买入，持有 N 个交易日后按收盘价卖出。
#
# 三条规则各自对应一个会让回测虚高的陷阱：
#
# ① **发布当日不买。** 当日收盘价里已经包含研报的影响，
#    用它买入等于假设你能在研报公开前拿到。
#
# ② **开盘涨停则顺延至下一交易日。** 开盘即涨停时买单排不进去，
#    用那个价格成交是虚构的收益。涨停幅度按板块与日期区分：
#      科创板 688      20%
#      创业板 300      20%（2020-08-24 起，此前 10%）
#      北交所 4/8 开头  30%
#      其余主板        10%
#    ⚠️ **ST 股的 5% 限制未处理**——`stock_basic.name` 只有当前名称，
#    没有历史 ST 状态。这会让个别 ST 股在实际涨停时被判为可买，
#    方向是**高估**收益。已知偏差，记录在此。
#
# ③ **连续涨停超过 5 个交易日则放弃该笔。** 你确实买不进去，
#    记为未成交而非按第 6 天的价格成交。实测 18147 份研报中
#    仅 7 笔属此情况。
#
# 卖出侧的跌停限制**未建模**：极端行情下当日可能无法卖出。
# 该偏差同样是高估方向，但影响远小于买入侧（卖出可分多日完成）。
ENTRY_CTE = """
WITH q AS (
    SELECT code, trade_date, open, close, adj_factor,
           lag(close) OVER (PARTITION BY code ORDER BY trade_date) AS prev_close,
           row_number() OVER (PARTITION BY code ORDER BY trade_date) AS rn
    FROM daily_quote
    WHERE close IS NOT NULL AND open IS NOT NULL
      AND adj_factor IS NOT NULL AND close > 0
),
lim AS (
    SELECT *,
           CASE
               WHEN code LIKE '688%' THEN 0.20
               WHEN code LIKE '300%' AND trade_date >= DATE '2020-08-24' THEN 0.20
               WHEN code LIKE '8%' OR code LIKE '4%' THEN 0.30
               ELSE 0.10
           END AS lim_pct
    FROM q
),
buyable AS (
    SELECT *,
           prev_close IS NOT NULL
             AND open < round(prev_close * (1 + lim_pct), 2) - 0.005 AS can_buy
    FROM lim
),
mx AS (SELECT code, max(rn) AS last_rn FROM q GROUP BY code),
cand AS (
    SELECT r.code, r.publish_date, r.institution, r.title, r.rating,
           b.rn, b.trade_date, b.open, b.adj_factor, b.can_buy,
           row_number() OVER (
               PARTITION BY r.code, r.publish_date, r.institution, r.title
               ORDER BY b.trade_date
           ) AS seq
    FROM research_report r
    JOIN buyable b ON b.code = r.code AND b.trade_date > r.publish_date
    WHERE r.rating IS NOT NULL AND r.rating <> ''
),
picked AS (
    SELECT *,
           min(CASE WHEN can_buy THEN seq END) OVER (
               PARTITION BY code, publish_date, institution, title
           ) AS ok_seq
    FROM cand WHERE seq <= 6
),
entry AS (
    SELECT code, publish_date, institution, title, rating,
           rn AS ern, trade_date AS buy_date, open AS buy_px,
           -- 开盘价的后复权值：因子按日给定，开盘与收盘同因子
           open * adj_factor AS buy_adj,
           seq - 1 AS deferred
    FROM picked WHERE seq = ok_seq
)
"""


@app.get("/api/institution-curves")
def institution_curves(
    horizon: int = 20,
    capital: float = 10000,
    min_trades: int = 100,
) -> dict:
    """按机构聚合的资金曲线 —— 「跟着这家机构的每一份研报买」的结果。

    每份研报投入 `capital` 元，按 ENTRY_CTE 的规则入场，
    持有 `horizon` 个交易日后按收盘价卖出。曲线按**月**聚合累计盈亏。

    **曲线的斜率才是信息，终点高度不是。** 发研报多的机构投入也多，
    终点自然更高；同时给出「累计收益率 = 累计盈亏 ÷ 累计投入」，
    那个才可跨机构比较。
    """
    with read_db() as db:
        trades = db.query(
            ENTRY_CTE + """
            SELECT e.institution AS 机构,
                   e.publish_date,
                   e.buy_date  AS 买入日,
                   x.trade_date AS 卖出日,
                   (x.close * x.adj_factor / e.buy_adj) - 1 AS ret,
                   e.deferred
            FROM entry e
            JOIN mx ON mx.code = e.code
            JOIN q x ON x.code = e.code AND x.rn = e.ern + ?
            WHERE e.ern + ? <= mx.last_rn
            """, [horizon, horizon])

    if trades.empty:
        return {"horizon": horizon, "capital": capital, "institutions": []}

    trades["月"] = pd.to_datetime(trades["publish_date"]).dt.to_period("M").astype(str)
    trades["盈亏"] = trades["ret"] * capital

    out = []
    for inst, g in trades.groupby("机构"):
        if len(g) < min_trades:
            continue
        m = (g.groupby("月")
               .agg(笔数=("ret", "size"), 盈亏=("盈亏", "sum"))
               .reset_index().sort_values("月"))
        m["累计盈亏"] = m["盈亏"].cumsum().round(0)
        m["累计笔数"] = m["笔数"].cumsum()
        m["累计投入"] = m["累计笔数"] * capital
        m["累计收益率"] = m["累计盈亏"] / m["累计投入"]

        use = _capital_usage(g["买入日"], g["卖出日"], capital)
        m["累计收益率"] = m["累计盈亏"] / use["占用资金"]

        ret = g["ret"]
        wins = int((ret > 0).sum())
        out.append({
            "机构": inst,
            "笔数": len(g),
            "总盈亏": float(g["盈亏"].sum()),
            "占用资金": use["占用资金"],
            "峰值并发": use["峰值并发"],
            "平均并发": use["平均并发"],
            "周转次数": use["周转次数"],
            # 收益率以**峰值占用资金**为分母：那才是你真正要准备的钱
            "累计收益率": float(g["盈亏"].sum() / use["占用资金"]),
            "年数": use["年数"],
            "年化收益率": _annualized(float(g["盈亏"].sum() / use["占用资金"]), use["年数"]),
            "胜率": wins / len(g),
            "平均收益率": float(ret.mean()),
            "中位收益率": float(ret.median()),
            "盈亏比": (float(ret[ret > 0].mean() / abs(ret[ret < 0].mean()))
                       if (ret < 0).any() and (ret > 0).any() else None),
            "涨停顺延笔数": int((g["deferred"] > 0).sum()),
            "最大回撤": _max_drawdown(m["累计盈亏"].to_numpy(), use["占用资金"]),
            "曲线": _records(m[["月", "累计盈亏", "累计收益率", "累计笔数"]]),
        })

    out.sort(key=lambda x: x["年化收益率"] or -1.0, reverse=True)

    # 基准线：不挑机构，**每一份研报都买**。
    #
    # 缺了它这张图会误导：2017–2026 市场整体上行，绝对收益人人为正，
    # 25 家里 23 家赚钱看起来像「研报普遍有用」。真正的问题是
    # 「挑这家」比「谁的都买」好多少——若各家曲线都贴着基准，
    # 那么挑机构这个动作本身没有创造任何东西。
    bm = (trades.groupby("月")
                .agg(笔数=("ret", "size"), 盈亏=("盈亏", "sum"))
                .reset_index().sort_values("月"))
    bm["累计盈亏"] = bm["盈亏"].cumsum().round(0)
    bm["累计笔数"] = bm["笔数"].cumsum()
    buse = _capital_usage(trades["买入日"], trades["卖出日"], capital)
    bm["累计收益率"] = bm["累计盈亏"] / buse["占用资金"]
    bret = trades["ret"]
    benchmark = {
        "机构": "全样本（每份都买）",
        "笔数": len(trades),
        "总盈亏": float(trades["盈亏"].sum()),
        "占用资金": buse["占用资金"],
        "峰值并发": buse["峰值并发"],
        "平均并发": buse["平均并发"],
        "周转次数": buse["周转次数"],
        "累计收益率": float(trades["盈亏"].sum() / buse["占用资金"]),
        "年数": buse["年数"],
        "年化收益率": _annualized(float(trades["盈亏"].sum() / buse["占用资金"]), buse["年数"]),
        "胜率": float((bret > 0).mean()),
        "平均收益率": float(bret.mean()),
        "中位收益率": float(bret.median()),
        "盈亏比": (float(bret[bret > 0].mean() / abs(bret[bret < 0].mean()))
                   if (bret < 0).any() and (bret > 0).any() else None),
        "涨停顺延笔数": int((trades["deferred"] > 0).sum()),
        "最大回撤": _max_drawdown(bm["累计盈亏"].to_numpy(), buse["占用资金"]),
        "曲线": _records(bm[["月", "累计盈亏", "累计收益率", "累计笔数"]]),
    }

    # 超过基准的家数：这个数字若接近半数，说明机构间差异与随机无异。
    #
    # 必须按**年化**比较，不能按累计：各家研报的时间跨度差异很大
    # （新时代证券仅 3.9 年，东吴证券 9.6 年），累计收益率把
    # 「九年赚 50%」与「四年赚 36%」显示成后者更差，那是错的。
    bm_ann = benchmark["年化收益率"] or -1.0
    beat = sum(1 for x in out if (x["年化收益率"] or -1.0) > bm_ann)

    return {
        "horizon": horizon,
        "capital": capital,
        "min_trades": min_trades,
        "entry_rule": "研报发布次日开盘价买入；开盘涨停顺延，连续涨停超 5 日放弃",
        "benchmark": benchmark,
        "beat_benchmark": beat,
        "institutions": out,
    }


def _annualized(total_return: float, years: float) -> float | None:
    """年化收益率。

    跨机构比较必须用它：各家研报的时间跨度不同，
    累计收益率把「九年赚 50%」与「三年赚 50%」显示成同一个数。

    ⚠️ 这是**绝对收益**，含市场 beta。2017–2026 A 股整体上行，
    正的年化不代表研报有价值——须与「每份都买」的基准对比才有意义。
    """
    if years <= 0 or total_return <= -1:
        return None
    return (1 + total_return) ** (1 / years) - 1


def _capital_usage(buys, sells, capital: float) -> dict:
    """计算这组交易真正占用了多少本金。

    **不能用「每笔 1 万 × 笔数」当投入。** 持有 20 个交易日后卖出，
    那笔钱就回到手里，下一份研报用的是同一笔钱。按笔数累加会得出
    东吴证券需要 2726 万本金的荒谬结论——实际同时在手的仓位远少于此。

    真正要准备的钱 = **同时持仓的峰值** × 单笔金额。
    用扫描线求任一时刻的在手笔数：买入 +1，卖出 +1 天后 −1。

    另给出平均并发与周转次数：
      周转 = 名义累计投入 ÷ 峰值占用，即这笔钱被复用了多少轮。
    """
    events: list[tuple[Any, int]] = []
    for b, sl in zip(buys, sells, strict=True):
        events.append((pd.Timestamp(b), 1))
        # 卖出当日资金即回笼，视为该日收盘后释放
        events.append((pd.Timestamp(sl), -1))
    events.sort(key=lambda e: (e[0], -e[1]))

    cur = peak = 0
    # 面积法求平均并发：Σ(持仓数 × 持续天数) ÷ 总天数
    area = 0.0
    prev = events[0][0] if events else None
    for t, d in events:
        if prev is not None and t > prev:
            area += cur * (t - prev).days
        cur += d
        peak = max(peak, cur)
        prev = t
    span = (events[-1][0] - events[0][0]).days if len(events) > 1 else 1

    peak_cap = peak * capital
    years = span / 365.25
    nominal = len(buys) * capital
    return {
        "峰值并发": peak,
        "占用资金": peak_cap,
        "平均并发": round(area / span, 2) if span > 0 else 0.0,
        "名义累计": nominal,
        "周转次数": round(nominal / peak_cap, 1) if peak_cap else None,
        "年数": round(years, 1),
    }


def _max_drawdown(cum: Any, total_capital: float) -> float:
    """资金曲线的最大回撤（占总投入的比例）。

    以**累计盈亏**而非净值计算：本策略的投入随研报数量增长，
    没有固定本金，净值口径无从定义。回撤除以总投入，
    得到「最坏时点亏掉了总投入的百分之几」。
    """
    if len(cum) == 0 or total_capital <= 0:
        return 0.0
    peak = cum[0]
    worst = 0.0
    for v in cum:
        peak = max(peak, v)
        worst = min(worst, v - peak)
    return float(worst / total_capital)


@app.get("/api/institution-trades")
def institution_trades(
    institution: str,
    horizon: int = 20,
    capital: float = 10000,
) -> dict:
    """某机构的每一份研报作为一笔交易，逐笔列出。

    入场规则与 `/api/institution-curves` 完全一致（见 ENTRY_CTE）：
    发布次日开盘价买入，开盘涨停顺延，连续涨停超 5 日放弃。
    两个接口共用同一段 SQL，避免同一页出现互相矛盾的数字。

    **买入价/卖出价是原始成交价，收益率用后复权。** 前者要与你券商
    App 看到的一致，后者必须正确处理持有期内的除权除息。二者对不上时
    `除权` 为真，差额即分红送转——那部分钱确实拿到了，只是不在价格里。
    """
    with read_db() as db:
        trades = db.query(
            ENTRY_CTE + """
            SELECT e.publish_date        AS 发布日,
                   e.code                AS 代码,
                   b.name                AS 股票,
                   e.title               AS 标题,
                   e.rating              AS 评级,
                   e.buy_date            AS 买入日,
                   e.buy_px              AS 买入价,
                   e.deferred            AS 顺延,
                   x.trade_date          AS 卖出日,
                   x.close               AS 卖出价,
                   (x.close * x.adj_factor / e.buy_adj) - 1 AS 收益率,
                   ((x.close * x.adj_factor / e.buy_adj) - 1) * ? AS 盈亏,
                   abs((x.close * x.adj_factor / e.buy_adj)
                       - (x.close / e.buy_px)) > 0.001 AS 除权
            FROM entry e
            JOIN mx ON mx.code = e.code
            JOIN q x ON x.code = e.code AND x.rn = e.ern + ?
            LEFT JOIN stock_basic b ON b.code = e.code
            WHERE e.institution = ? AND e.ern + ? <= mx.last_rn
            ORDER BY e.publish_date DESC
            """, [capital, horizon, institution, horizon])

    if trades.empty:
        return {"institution": institution, "horizon": horizon,
                "capital": capital, "summary": None, "trades": []}

    use = _capital_usage(trades["买入日"], trades["卖出日"], capital)
    ret = trades["收益率"]
    wins = int((ret > 0).sum())
    total = float(trades["盈亏"].sum())

    curve = trades.sort_values("发布日")[["发布日", "盈亏"]].copy()
    curve["累计"] = curve["盈亏"].cumsum()

    summary = {
        "笔数": len(trades),
        "总盈亏": total,
        # ⚠️ 不是 capital × 笔数。持有期满卖出后资金回笼，
        #    下一笔用的是同一笔钱；真正要准备的是**峰值同时持仓**。
        "占用资金": use["占用资金"],
        "峰值并发": use["峰值并发"],
        "平均并发": use["平均并发"],
        "周转次数": use["周转次数"],
        "年数": use["年数"],
        "累计收益率": total / use["占用资金"] if use["占用资金"] else None,
        "年化收益率": _annualized(total / use["占用资金"], use["年数"])
                      if use["占用资金"] else None,
        "最大回撤": _max_drawdown(curve["累计"].to_numpy(), use["占用资金"]),
        "胜率": wins / len(trades),
        "盈利笔数": wins,
        "亏损笔数": len(trades) - wins,
        "平均收益率": float(ret.mean()),
        "中位收益率": float(ret.median()),
        "最好": float(ret.max()),
        "最差": float(ret.min()),
        "盈亏比": (float(ret[ret > 0].mean() / abs(ret[ret < 0].mean()))
                   if (ret < 0).any() and (ret > 0).any() else None),
        "含除权笔数": int(trades["除权"].sum()),
        "涨停顺延笔数": int((trades["顺延"] > 0).sum()),
    }

    return {
        "institution": institution,
        "horizon": horizon,
        "capital": capital,
        "summary": summary,
        "curve": _records(curve),
        "trades": _records(trades),
    }


@app.get("/api/institution-options")
def institution_options(min_reports: int = 30) -> list[dict]:
    """有足够样本的机构名单，供逐笔复盘页下拉选择。"""
    with read_db() as db:
        return _records(db.query(
            """
            SELECT institution AS 机构, count(*) AS 研报数,
                   min(publish_date) AS 最早, max(publish_date) AS 最晚
            FROM research_report
            WHERE rating IS NOT NULL AND rating <> ''
            GROUP BY 1 HAVING count(*) >= ?
            ORDER BY count(*) DESC
            """, [min_reports]))


@app.get("/api/institution-winrates")
def institution_winrates(
    horizon: Annotated[int, Query(ge=20, le=250, description="考察窗口，交易日")] = 60,
    min_samples: Annotated[int, Query(ge=3)] = 10,
) -> dict:
    """各券商在近一年 / 近两年 / 近三年的胜率对比。

    **两个必须理解的设计约束：**

    1. **窗口默认 60 日而非 250 日。** 若用 250 日（约一年），
       「近一年发布的研报」中绝大多数尚未走完窗口，会被全部剔除，
       该时段样本近乎归零。60 日（约三个月）能让三个时段都有可比样本。

    2. **仍然排除未走完窗口的研报。** 发布日 + horizon 若超出数据末日，
       该研报尚无结果；纳入统计会依最近行情涨跌而系统性偏移。

    胜率以**超额收益**为准（个股收益 − 同月全样本中位数）：
    用绝对收益的话，牛市里所有机构都「赢」，那不是研报的功劳。

    ⚠️ 已实测机构排名的跨期秩相关为 −0.286（比随机更差）。
    本表用于查看历史分布，**不可外推为未来的机构权重**。
    """
    with read_db() as db:
        end = db.query(
            "SELECT max(trade_date) AS d FROM daily_quote")["d"].iloc[0]
        end_date = pd.Timestamp(end).date()

        rows = db.query(
            """
            WITH px AS (
                SELECT code, trade_date, close * adj_factor AS p,
                       row_number() OVER (PARTITION BY code ORDER BY trade_date) AS rn
                FROM daily_quote
                WHERE close IS NOT NULL AND adj_factor IS NOT NULL AND close > 0
            ),
            maxrn AS (SELECT code, max(rn) AS last_rn FROM px GROUP BY code),
            ent AS (
                SELECT r.code, r.publish_date, r.institution,
                       min(px.rn) AS entry_rn
                FROM research_report r
                JOIN px ON px.code = r.code AND px.trade_date > r.publish_date
                WHERE r.rating IS NOT NULL AND r.rating <> ''
                GROUP BY r.code, r.publish_date, r.institution
            ),
            outcome AS (
                SELECT e.publish_date, e.institution,
                       (h.p / en.p) - 1 AS ret
                FROM ent e
                JOIN maxrn m ON m.code = e.code
                JOIN px en ON en.code = e.code AND en.rn = e.entry_rn
                JOIN px h  ON h.code  = e.code AND h.rn  = e.entry_rn + ?
                -- 窗口必须已走完，否则该研报尚无结果
                WHERE e.entry_rn + ? <= m.last_rn
            ),
            benched AS (
                SELECT o.*,
                       o.ret - median(o.ret) OVER (
                           PARTITION BY date_trunc('month', o.publish_date)
                       ) AS excess
                FROM outcome o
            )
            SELECT institution AS 机构,
                   -- 三个时段各自的样本量与胜率
                   count(*) FILTER (WHERE publish_date >= ? - INTERVAL 1 YEAR)  AS n1,
                   count(*) FILTER (WHERE publish_date >= ? - INTERVAL 2 YEAR)  AS n2,
                   count(*) FILTER (WHERE publish_date >= ? - INTERVAL 3 YEAR)  AS n3,
                   avg(CASE WHEN excess > 0 THEN 1.0 ELSE 0.0 END)
                       FILTER (WHERE publish_date >= ? - INTERVAL 1 YEAR)       AS w1,
                   avg(CASE WHEN excess > 0 THEN 1.0 ELSE 0.0 END)
                       FILTER (WHERE publish_date >= ? - INTERVAL 2 YEAR)       AS w2,
                   avg(CASE WHEN excess > 0 THEN 1.0 ELSE 0.0 END)
                       FILTER (WHERE publish_date >= ? - INTERVAL 3 YEAR)       AS w3,
                   median(excess) FILTER (WHERE publish_date >= ? - INTERVAL 1 YEAR) AS e1,
                   median(excess) FILTER (WHERE publish_date >= ? - INTERVAL 2 YEAR) AS e2,
                   median(excess) FILTER (WHERE publish_date >= ? - INTERVAL 3 YEAR) AS e3
            FROM benched
            GROUP BY institution
            HAVING count(*) FILTER (WHERE publish_date >= ? - INTERVAL 3 YEAR) >= ?
            ORDER BY w3 DESC NULLS LAST
            """,
            [horizon, horizon] + [end_date] * 10 + [min_samples],
        )

        return {
            "horizon": horizon,
            "as_of": str(end_date),
            "total": len(rows),
            "rows": _records(rows),
            "caveat": (
                f"窗口 {horizon} 交易日；已排除窗口未走完的研报。"
                "胜率以超额收益（个股收益 − 同月全样本中位数）为准。"
                "⚠️ 机构排名跨期秩相关实测 −0.286，本表不可外推为未来权重。"
            ),
        }
