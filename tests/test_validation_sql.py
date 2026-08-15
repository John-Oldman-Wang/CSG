"""验证①②③ 的 SQL 与计算逻辑校验（合成数据）。

**为什么必须提前测**：真实数据采集以小时计，若等数据到位才发现
SQL 语法错误或 join 串行，代价是重跑整个流程。今晚研报回测正是
靠这种预检提前暴露了问题。

合成数据的作用是验证**计算是否正确**，不是验证结论——
结论必须来自真实数据。因此这里的断言都针对可精确推导的中间量：
累计值还原、PIT 过滤边界、幸存者偏差处理。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from csg.analysis import flags, metrics
from csg.validation import cycle_study, flag_backtest
from csg.validation import screen_backtest as sb

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "src" / "csg" / "storage"


class MemDB:
    def __init__(self) -> None:
        self.conn = duckdb.connect(":memory:")
        for name in ("schema.sql", "schema_events.sql"):
            self.conn.execute((SCHEMA_DIR / name).read_text(encoding="utf-8"))

    def query(self, sql: str, params=None) -> pd.DataFrame:
        return self.conn.execute(sql, params or []).df()

    def close(self) -> None:
        self.conn.close()

    # 供 screen_backtest 调用
    def pit_universe(self, as_of: dt.date, *, exclude_st: bool = False) -> pd.DataFrame:
        return self.query(
            "SELECT code, name FROM stock_basic WHERE list_date <= ? "
            "AND (delist_date IS NULL OR delist_date > ?)", [as_of, as_of])

    def pit_financials(self, as_of, *, table="fin_income", codes=None,
                       lookback_periods=1) -> pd.DataFrame:
        sql = f"""
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY code ORDER BY report_period DESC) AS _rn
                FROM {table}
                WHERE disclosure_date IS NOT NULL AND disclosure_date <= ?
            ) WHERE _rn <= ?
        """
        df = self.query(sql, [as_of, lookback_periods]).drop(columns=["_rn"])
        if codes is not None:
            df = df[df["code"].isin(list(codes))]
        return df


@pytest.fixture
def db() -> MemDB:
    d = MemDB()
    d.conn.executemany(
        "INSERT INTO stock_basic (code,name,market,exchange,list_date,"
        "delist_date,is_active) VALUES (?,?,?,?,?,?,?)",
        [
            ("300001", "正常公司", "创业板", "SZ", dt.date(2015, 1, 1), None, True),
            ("300002", "暴雷公司", "创业板", "SZ", dt.date(2015, 1, 1), None, True),
            # 已退市：用于验证幸存者偏差处理——今天的股票列表里没有它，
            # 但 2019 年回测时它必须在池内
            ("300003", "退市公司", "创业板", "SZ", dt.date(2015, 1, 1),
             dt.date(2021, 6, 30), False),
        ])
    d.conn.executemany(
        "INSERT INTO industry_member (code,taxonomy,industry_name) VALUES (?,?,?)",
        [(c, "em_industry", "电池") for c in ("300001", "300002", "300003")])
    return d


def _fin_rows(code: str, year: int, revenue_q4: float, profit_q4: float):
    """按 A 股累计口径生成四期财报。年报为全年累计值。"""
    return [
        (code, dt.date(year, 3, 31), dt.date(year, 4, 20),
         revenue_q4 * 0.2, profit_q4 * 0.2),
        (code, dt.date(year, 6, 30), dt.date(year, 8, 20),
         revenue_q4 * 0.45, profit_q4 * 0.45),
        (code, dt.date(year, 9, 30), dt.date(year, 10, 25),
         revenue_q4 * 0.7, profit_q4 * 0.7),
        (code, dt.date(year, 12, 31), dt.date(year + 1, 4, 20),
         revenue_q4, profit_q4),
    ]


# ----------------------------------------------------------------------
# PIT 与累计值口径 —— 一切验证的地基
# ----------------------------------------------------------------------

def test_pit_excludes_undisclosed_reports(db: MemDB) -> None:
    """未披露的财报不得进入面板。

    2024 年报的披露日是 2025-04-20；在此之前查询绝不能看到它。
    这是 PIT 的全部要害——用错会静默产生未来函数，回测收益凭空变好。
    """
    db.conn.executemany(
        "INSERT INTO fin_income (code,report_period,disclosure_date,"
        "total_revenue,n_income_attr_p) VALUES (?,?,?,?,?)",
        _fin_rows("300001", 2024, 1e9, 1e8))

    before = metrics.load_pit_panel(db, dt.date(2025, 4, 19), codes=["300001"])
    after = metrics.load_pit_panel(db, dt.date(2025, 4, 21), codes=["300001"])

    periods_before = set(pd.to_datetime(before["report_period"]).dt.date)
    periods_after = set(pd.to_datetime(after["report_period"]).dt.date)

    assert dt.date(2024, 12, 31) not in periods_before, (
        "披露前不得可见 —— 否则即未来函数")
    assert dt.date(2024, 12, 31) in periods_after, "披露后应可见"


def test_cumulative_to_quarterly(db: MemDB) -> None:
    """累计值正确还原为单季值。

    A 股财报是年初至今累计：Q3 报表的营收是前三季合计。
    直接拿累计值算同比会得到无意义的结果。
    """
    db.conn.executemany(
        "INSERT INTO fin_income (code,report_period,disclosure_date,"
        "total_revenue,n_income_attr_p) VALUES (?,?,?,?,?)",
        _fin_rows("300001", 2024, 1000.0, 100.0))

    panel = metrics.load_pit_panel(db, dt.date(2025, 6, 1), codes=["300001"])
    q = panel.set_index(pd.to_datetime(panel["report_period"]).dt.date)

    # Q1 单季 = 累计值；Q2 单季 = 450 − 200 = 250
    assert q.loc[dt.date(2024, 3, 31), "total_revenue_q"] == pytest.approx(200.0)
    assert q.loc[dt.date(2024, 6, 30), "total_revenue_q"] == pytest.approx(250.0)
    assert q.loc[dt.date(2024, 9, 30), "total_revenue_q"] == pytest.approx(250.0)
    assert q.loc[dt.date(2024, 12, 31), "total_revenue_q"] == pytest.approx(300.0)


def test_pit_universe_includes_delisted(db: MemDB) -> None:
    """时点股票池必须包含「当时在市、此后退市」的公司。

    用今天的股票列表回测历史会自动剔除退市股，凭空美化收益——
    这是回测最经典的偏差来源。
    """
    in_2019 = db.pit_universe(dt.date(2019, 6, 30))
    in_2024 = db.pit_universe(dt.date(2024, 6, 30))

    assert "300003" in set(in_2019["code"]), "2019 年该股在市，必须在池内"
    assert "300003" not in set(in_2024["code"]), "2021 年退市后不应在池内"


# ----------------------------------------------------------------------
# 验证① 红旗规则
# ----------------------------------------------------------------------

def test_blowup_detection_uses_disclosure_date(db: MemDB) -> None:
    """暴雷事件以**披露日**为事件日期，而非报告期。

    投资者是在财报公布那天才知道业绩崩塌的，不是报告期结束那天。
    用报告期当事件日，等于假设人能提前三个月知道结果。
    """
    db.conn.executemany(
        "INSERT INTO fin_income (code,report_period,disclosure_date,"
        "total_revenue,n_income_attr_p) VALUES (?,?,?,?,?)",
        [("300002", dt.date(2022, 12, 31), dt.date(2023, 4, 20), 1e9, 5e7),
         # 次年营收腰斩
         ("300002", dt.date(2023, 12, 31), dt.date(2024, 4, 25), 3e8, -2e8)])
    db.conn.execute(
        "INSERT INTO fin_balance (code,report_period,disclosure_date,"
        "total_assets,total_liab,equity_attr_p) VALUES "
        "('300002','2023-12-31','2024-04-25', 1e9, 5e8, 5e8)")

    events = flag_backtest.find_blowup_events(db, start=dt.date(2020, 1, 1))
    assert not events.empty, "应识别出暴雷事件"

    hit = events[events["code"] == "300002"]
    assert not hit.empty
    ev_date = pd.to_datetime(hit["event_date"].iloc[0]).date()
    assert ev_date == dt.date(2024, 4, 25), (
        "事件日期须为披露日 2024-04-25，而非报告期 2023-12-31")


def test_flag_evaluation_runs_on_pit_panel(db: MemDB) -> None:
    """红旗规则可在 PIT 面板上计算，输出结构完整。"""
    db.conn.executemany(
        "INSERT INTO fin_income (code,report_period,disclosure_date,"
        "total_revenue,n_income_attr_p) VALUES (?,?,?,?,?)",
        _fin_rows("300001", 2023, 1e9, 1e8) + _fin_rows("300001", 2024, 1.1e9, 1.1e8))
    for year in (2023, 2024):
        db.conn.execute(
            "INSERT INTO fin_balance (code,report_period,disclosure_date,"
            "total_assets,total_liab,equity_attr_p,goodwill,accounts_receiv,"
            "inventories) VALUES (?,?,?,?,?,?,?,?,?)",
            ["300001", dt.date(year, 12, 31), dt.date(year + 1, 4, 20),
             2e9, 1.6e9, 4e8, 2e8, 3e8, 4e8])   # 负债率 80%、商誉/净资产 50%

    panel = metrics.load_pit_panel(db, dt.date(2025, 6, 1), codes=["300001"])
    rated = flags.evaluate(metrics.compute_ratios(panel))

    assert "flag_score" in rated.columns
    assert "flag_names" in rated.columns
    latest = metrics.latest_snapshot(rated).iloc[0]
    assert latest["flag_count"] >= 1, "高负债 + 高商誉应触发红旗"


# ----------------------------------------------------------------------
# 验证② 行业周期
# ----------------------------------------------------------------------

def test_industry_aggregate_sql(db: MemDB) -> None:
    """行业聚合 SQL 可执行；样本不足的报告期被 HAVING 过滤。

    聚合口径为行业**合计**而非个股均值——均值会被小公司的极端比率主导。
    """
    for code in ("300001", "300002", "300003"):
        db.conn.executemany(
            "INSERT INTO fin_income (code,report_period,disclosure_date,"
            "total_revenue,operating_cost,n_income_attr_p) VALUES (?,?,?,?,?,?)",
            [(code, dt.date(2023, 12, 31), dt.date(2024, 4, 20), 1e9, 8e8, 1e8)])

    agg = cycle_study.industry_aggregate(db, ["电池"])
    # 仅 3 家公司，低于 HAVING count >= 5 的门槛
    assert agg.empty, "样本不足的报告期应被过滤，避免用 3 家公司代表行业"


def test_cycle_indicators_computed() -> None:
    """周期指标计算正确（不依赖数据库）。"""
    agg = pd.DataFrame({
        "industry_name": ["电池"] * 3,
        "report_period": [dt.date(2022, 12, 31), dt.date(2023, 12, 31),
                          dt.date(2024, 12, 31)],
        "companies": [10, 10, 10],
        "last_disclosure": [dt.date(2023, 4, 20)] * 3,
        "revenue": [1000.0, 1200.0, 900.0],
        "cost": [700.0, 900.0, 800.0],
        "net_profit": [200.0, 180.0, 20.0],
        "inventories": [200.0, 300.0, 400.0],
        "fix_assets": [500.0, 600.0, 700.0],
        "cip": [100.0, 200.0, 50.0],
        "capex": [150.0, 250.0, 60.0],
        "cfo": [220.0, 150.0, 30.0],
    })
    ind = cycle_study.cycle_indicators(agg)

    assert len(ind) == 3, "三期年报都应保留"
    # 2022: 毛利率 = 1 − 700/1000 = 0.30
    assert ind.iloc[0]["gross_margin"] == pytest.approx(0.30)
    # 2024 毛利率跌到 1 − 800/900 ≈ 0.111 —— 典型的价格战特征
    assert ind.iloc[2]["gross_margin"] < ind.iloc[0]["gross_margin"]
    # 存货占比逐年抬升 —— 产能过剩前兆
    assert ind.iloc[2]["inventory_ratio"] > ind.iloc[0]["inventory_ratio"]


# ----------------------------------------------------------------------
# 验证③ 筛选回测
# ----------------------------------------------------------------------

def test_screen_rule_filters(db: MemDB) -> None:
    """筛选条件按预期过滤，并受红旗分值上限约束。"""
    snap = pd.DataFrame({
        "code": ["A", "B", "C"],
        "roe_ttm": [0.20, 0.05, 0.25],
        "cfo_to_ni": [1.2, 1.5, 0.3],
        "debt_ratio": [0.4, 0.3, 0.5],
        "flag_score": [0, 0, 5],
    })
    rule = sb.ScreenRule(
        conditions=[("roe_ttm", ">", 0.12), ("cfo_to_ni", ">", 0.6)],
        max_flag_score=3, top_n=10)
    picked = rule.apply(snap)

    assert set(picked["code"]) == {"A"}, (
        "B 因 ROE 不达标出局；C 虽 ROE 最高但红旗分值超限且现金流差")


def test_backtest_reports_no_data_gracefully(db: MemDB) -> None:
    """无数据时给出明确说明，而非抛异常或返回虚假的零收益。"""
    detail, summary = sb.run_backtest(
        db, sb.ScreenRule(), sb.BacktestConfig(
            start=dt.date(2020, 1, 1), end=dt.date(2020, 12, 31)))
    assert detail.empty
    assert "说明" in summary, "空结果必须携带解释，不能静默返回 0"


# ----------------------------------------------------------------------
# 存储层回归
# ----------------------------------------------------------------------

def test_upsert_is_idempotent_on_indexed_table() -> None:
    """upsert 在带主键索引的表上必须幂等。

    **回归保护**：早期实现用 `DELETE ... USING` + `INSERT`，
    在带主键索引的表上会失败：

        Invalid Input Error: Failed to delete all rows from index.
        Only deleted 0 out of 82 rows.

    该错误在采集途中抛出会终止整轮任务——曾因此中断一次财务采集。
    现改用 DuckDB 原生的 INSERT OR REPLACE。
    """
    from csg.storage.db import Database

    db = Database(":memory:")
    db.init_schema()
    rows = pd.DataFrame({
        "code": ["002150"] * 82,
        "report_period": pd.bdate_range("2005-01-01", periods=82, freq="QE").date,
        "disclosure_date": pd.bdate_range("2005-04-01", periods=82, freq="QE").date,
        "total_assets": [None] * 82,
        "share_capital": [None] * 82,
    })

    db.upsert("fin_balance", rows, ["code", "report_period"])
    db.upsert("fin_balance", rows, ["code", "report_period"])   # 重复写入

    total = db.query("SELECT count(*) n FROM fin_balance")["n"].iloc[0]
    assert total == 82, f"重复 upsert 应保持 82 行，实得 {total}"

    updated = rows.copy()
    updated["total_assets"] = 12345.0
    db.upsert("fin_balance", updated, ["code", "report_period"])
    vals = db.query("SELECT DISTINCT total_assets FROM fin_balance")["total_assets"]
    assert vals.tolist() == [12345.0], "覆盖更新未生效"
    db.close()


def test_upsert_rejects_missing_key_column() -> None:
    """缺少主键列时明确报错，而非静默写入错误数据。"""
    from csg.storage.db import Database

    db = Database(":memory:")
    db.init_schema()
    with pytest.raises(ValueError, match="缺少主键列"):
        db.upsert("fin_balance", pd.DataFrame({"code": ["000001"]}),
                  ["code", "report_period"])
    db.close()
