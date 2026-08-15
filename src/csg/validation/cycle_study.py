"""验证② 新能源行业周期规律。

**要回答的核心问题**：用 PE 分位筛选周期行业，是否会系统性在周期高点买入？

理论预期是「会」——周期行业在盈利顶部时 PE 最低，看起来最便宜的时刻
恰恰是最危险的时刻。但这需要用数据确认，而非凭理论断言。

本模块不预测行业走向，只做历史规律的**描述统计**，
产出用于设计周期行业的指标集（METHODOLOGY 提到的「不同行业不同指标」）。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from csg.analysis import metrics
from csg.storage import Database


def industry_aggregate(
    db: Database,
    industries: list[str],
    *,
    start: dt.date = dt.date(2016, 1, 1),
    end: dt.date | None = None,
) -> pd.DataFrame:
    """行业层财务聚合，按报告期。

    用**披露日**而非报告期作为可见性判断：某报告期的行业汇总，
    只有当期内多数公司都已披露后才真正可得。这里保留 report_period
    做横向对比，同时记录该期最晚披露日，供 PIT 场景使用。

    聚合口径为「行业合计」而非「个股均值」——均值会被小公司的
    极端比率主导，合计才反映行业真实的产能与盈利状况。
    """
    end = end or dt.date.today()
    placeholders = ", ".join("?" * len(industries))

    return db.query(
        f"""
        WITH pool AS (
            SELECT DISTINCT b.code, i.industry_name
            FROM stock_basic b
            JOIN industry_member i ON i.code = b.code
            WHERE i.taxonomy = 'em_industry'
              AND i.industry_name IN ({placeholders})
        )
        SELECT p.industry_name,
               inc.report_period,
               count(DISTINCT inc.code)                AS companies,
               max(inc.disclosure_date)                AS last_disclosure,
               sum(inc.total_revenue)                  AS revenue,
               sum(inc.operating_cost)                 AS cost,
               sum(inc.n_income_attr_p)                AS net_profit,
               sum(bal.inventories)                    AS inventories,
               sum(bal.fix_assets)                     AS fix_assets,
               sum(bal.cip)                            AS cip,
               sum(cf.c_pay_acq_const)                 AS capex,
               sum(cf.n_cashflow_act)                  AS cfo
        FROM fin_income inc
        JOIN pool p           ON p.code = inc.code
        LEFT JOIN fin_balance bal
               ON bal.code = inc.code AND bal.report_period = inc.report_period
        LEFT JOIN fin_cashflow cf
               ON cf.code = inc.code AND cf.report_period = inc.report_period
        WHERE inc.report_period BETWEEN ? AND ?
        GROUP BY p.industry_name, inc.report_period
        HAVING count(DISTINCT inc.code) >= 5
        ORDER BY p.industry_name, inc.report_period
        """,
        [*industries, start, end],
    )


def cycle_indicators(agg: pd.DataFrame) -> pd.DataFrame:
    """行业周期观察指标。

    这些指标共同刻画产能周期的位置：
      扩产 → 产能释放 → 供过于求 → 价格战 → 毛利下滑 → 存货堆积 → 出清
    """
    if agg.empty:
        return agg

    df = agg.sort_values(["industry_name", "report_period"]).copy()
    # 年报为累计口径，此处仅用年报（Q4）做年度对比，避免累计/单季混淆
    df["is_annual"] = pd.to_datetime(df["report_period"]).dt.month == 12

    df["gross_margin"] = 1 - df["cost"] / df["revenue"].where(df["revenue"] > 0)
    df["net_margin"] = df["net_profit"] / df["revenue"].where(df["revenue"] > 0)
    df["capex_intensity"] = df["capex"] / df["revenue"].where(df["revenue"] > 0)
    df["cip_ratio"] = df["cip"] / df["fix_assets"].where(df["fix_assets"] > 0)
    df["inventory_ratio"] = df["inventories"] / df["revenue"].where(df["revenue"] > 0)
    df["cfo_to_profit"] = df["cfo"] / df["net_profit"].where(df["net_profit"].abs() > 1e-6)

    annual = df[df["is_annual"]].copy()
    for col in ["revenue", "net_profit", "capex"]:
        prev = annual.groupby("industry_name")[col].shift(1)
        annual[f"{col}_yoy"] = annual[col] / prev.where(prev.abs() > 1e-6) - 1

    return annual.reset_index(drop=True)


def pe_percentile_vs_forward_return(
    db: Database,
    codes: list[str],
    *,
    rebalance_months: int = 3,
    forward_months: int = 12,
    start: dt.date = dt.date(2017, 1, 1),
    end: dt.date | None = None,
) -> pd.DataFrame:
    """PE 分位与未来收益的关系 —— 本验证的核心检验。

    在每个调仓时点，按当时可见的 PE_TTM 分位分组，
    统计各组此后 `forward_months` 的收益。

    若「低 PE 分位组」的未来收益并不占优、甚至更差，
    即证实了周期行业的 PE 陷阱：盈利顶部 PE 最低，
    而那正是盈利即将下行的时点。

    PE 自行计算而非取用外部字段：PE = 市值 / 净利润TTM，
    其中净利润严格取当时已披露者，保证 PIT 一致性。
    需要 daily_basic 表提供市值数据。
    """
    end = end or dt.date.today()

    have_mv = db.query(
        "SELECT count(*) AS n FROM daily_basic WHERE total_mv IS NOT NULL"
    )["n"].iloc[0]
    if have_mv == 0:
        return pd.DataFrame({
            "说明": ["缺少市值数据：daily_basic 表为空。"
                     "需先补充市值采集（akshare 无稳定的历史 PE 批量接口，"
                     "计划由 tushare daily_basic 提供，待积分到位）"]
        })

    rows: list[dict] = []
    cursor = start
    while cursor < end:
        panel = metrics.load_pit_panel(db, cursor, codes=codes, periods=8)
        if not panel.empty:
            snap = metrics.latest_snapshot(metrics.compute_ratios(panel))
            mv = db.query(
                """
                SELECT code, total_mv FROM daily_basic
                WHERE trade_date = (
                    SELECT max(trade_date) FROM daily_basic WHERE trade_date <= ?
                )
                """,
                [cursor],
            )
            merged = snap.merge(mv, on="code", how="inner")
            merged["pe_ttm"] = merged["total_mv"] / merged["n_income_attr_p_ttm"].where(
                merged["n_income_attr_p_ttm"] > 0
            )
            valid = merged.dropna(subset=["pe_ttm"])
            if len(valid) >= 10:
                valid = valid.copy()
                valid["pe_bucket"] = pd.qcut(
                    valid["pe_ttm"], 5, labels=["最低20%", "20-40%", "40-60%",
                                                "60-80%", "最高20%"],
                    duplicates="drop",
                )
                fwd_end = cursor + pd.DateOffset(months=forward_months)
                ret = _forward_returns(db, valid["code"].tolist(), cursor,
                                       fwd_end.date())
                valid = valid.merge(ret, on="code", how="left")
                for bucket, grp in valid.groupby("pe_bucket", observed=True):
                    rows.append({
                        "as_of": cursor,
                        "pe_bucket": str(bucket),
                        "样本数": len(grp),
                        "中位PE": round(float(grp["pe_ttm"].median()), 1),
                        "未来收益中位数": round(float(grp["fwd_return"].median()), 4)
                        if grp["fwd_return"].notna().any() else None,
                    })

        cursor = (pd.Timestamp(cursor) + pd.DateOffset(months=rebalance_months)).date()

    return pd.DataFrame(rows)


def _forward_returns(
    db: Database, codes: list[str], start: dt.date, end: dt.date
) -> pd.DataFrame:
    """区间后复权收益率。

    后复权价不受未来除权事件影响，是唯一可用于历史收益计算的口径。
    """
    if not codes:
        return pd.DataFrame(columns=["code", "fwd_return"])

    placeholders = ", ".join("?" * len(codes))
    return db.query(
        f"""
        WITH px AS (
            SELECT code, trade_date, close * adj_factor AS p
            FROM daily_quote
            WHERE code IN ({placeholders}) AND trade_date BETWEEN ? AND ?
        ),
        bounds AS (
            SELECT code, min(trade_date) AS d0, max(trade_date) AS d1
            FROM px GROUP BY code
        )
        SELECT b.code,
               (p1.p / nullif(p0.p, 0)) - 1 AS fwd_return
        FROM bounds b
        JOIN px p0 ON p0.code = b.code AND p0.trade_date = b.d0
        JOIN px p1 ON p1.code = b.code AND p1.trade_date = b.d1
        """,
        [*codes, start, end],
    )
