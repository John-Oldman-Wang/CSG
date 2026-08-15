"""财务指标计算。

**两条硬约束：**

1. **Point-in-Time**：所有取数以 `disclosure_date <= as_of` 过滤，
   绝不使用 report_period。2024Q3 的报告期数据可能到 10-28 才公布，
   在此之前使用即未来函数。

2. **累计值口径**：A 股财报的利润表与现金流量表按**年初至今累计**披露。
   Q3 报表的「净利润」是前三季度合计，不是第三季度单季。
   直接对累计值求同比/环比会得到无意义的结果，必须先还原单季值。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import pandas as pd

from csg.storage import Database

# 累计口径字段（流量表）。资产负债表是时点值，不参与还原。
FLOW_FIELDS = {
    "total_revenue", "revenue", "operating_cost", "selling_exp", "admin_exp",
    "rd_exp", "fin_exp", "operate_profit", "total_profit", "income_tax",
    "n_income", "n_income_attr_p",
    "n_cashflow_act", "n_cashflow_inv_act", "n_cashflow_fin_act",
    "c_pay_acq_const", "free_cashflow",
}


def to_quarterly(df: pd.DataFrame, fields: Sequence[str]) -> pd.DataFrame:
    """累计值还原为单季值。

        Q1单季 = Q1累计
        Q2单季 = H1累计 − Q1累计
        Q3单季 = Q3累计 − H1累计
        Q4单季 = 年报   − Q3累计

    仅在同一年度内相减；跨年时 Q1 直接取累计值。
    缺失中间期时该季标记为 NA，不做插值——凭空补出来的数字比缺失更危险。
    """
    out = df.sort_values(["code", "report_period"]).copy()
    out["_year"] = pd.to_datetime(out["report_period"]).dt.year
    out["_q"] = pd.to_datetime(out["report_period"]).dt.quarter

    for f in fields:
        if f not in out.columns:
            continue
        prev = out.groupby(["code", "_year"])[f].shift(1)
        prev_q = out.groupby(["code", "_year"])["_q"].shift(1)
        # 仅当上一期恰为同年前一季时才相减，否则视为缺口
        contiguous = prev_q == (out["_q"] - 1)
        out[f"{f}_q"] = out[f].where(
            out["_q"] == 1,
            (out[f] - prev).where(contiguous),
        )

    return out.drop(columns=["_year", "_q"])


def add_ttm(df: pd.DataFrame, fields: Sequence[str], *, min_periods: int = 4) -> pd.DataFrame:
    """在单季值基础上计算 TTM（滚动四个季度求和）。

    要求 to_quarterly 已执行。不足四季返回 NA，不做年化外推——
    用两个季度年化推算全年，在周期行业里会产生严重误导。
    """
    out = df.sort_values(["code", "report_period"]).copy()
    for f in fields:
        col = f"{f}_q"
        if col not in out.columns:
            continue
        out[f"{f}_ttm"] = (
            out.groupby("code")[col]
            .rolling(4, min_periods=min_periods)
            .sum()
            .reset_index(level=0, drop=True)
        )
    return out


def load_pit_panel(
    db: Database,
    as_of: dt.date,
    codes: Sequence[str] | None = None,
    *,
    periods: int = 24,
) -> pd.DataFrame:
    """构建截至 `as_of` 时点、**当时可见**的财务面板。

    三张报表以 (code, report_period) 关联。
    过滤条件是 disclosure_date —— PIT 的全部要害所在。
    """
    frames = {}
    for table in ("fin_income", "fin_balance", "fin_cashflow"):
        df = db.pit_financials(as_of, table=table, codes=codes,
                               lookback_periods=periods)
        if not df.empty:
            frames[table] = df.drop(columns=["disclosure_date"], errors="ignore")

    if "fin_income" not in frames:
        return pd.DataFrame()

    panel = frames["fin_income"]
    for t in ("fin_balance", "fin_cashflow"):
        if t in frames:
            panel = panel.merge(frames[t], on=["code", "report_period"], how="outer")

    # 披露日期以利润表为准回填，供后续核验
    disc = db.pit_financials(as_of, table="fin_income", codes=codes,
                             lookback_periods=periods)[
        ["code", "report_period", "disclosure_date"]]
    panel = panel.merge(disc, on=["code", "report_period"], how="left")

    flow = [f for f in FLOW_FIELDS if f in panel.columns]
    panel = to_quarterly(panel, flow)
    panel = add_ttm(panel, flow)
    return panel.sort_values(["code", "report_period"]).reset_index(drop=True)


def compute_ratios(panel: pd.DataFrame) -> pd.DataFrame:
    """在 PIT 面板上计算派生比率。

    分母一律做零值保护：A 股中净利润为零或极小的样本足以产生
    上万倍的比率，污染分位数统计。
    """
    df = panel.copy()

    def safe_div(a: str, b: str) -> pd.Series:
        if a not in df.columns or b not in df.columns:
            return pd.Series(pd.NA, index=df.index, dtype="Float64")
        num, den = pd.to_numeric(df[a], errors="coerce"), pd.to_numeric(df[b], errors="coerce")
        return (num / den.where(den.abs() > 1e-6)).astype("Float64")

    # 盈利能力
    df["roe_ttm"] = safe_div("n_income_attr_p_ttm", "equity_attr_p")
    df["net_margin_ttm"] = safe_div("n_income_ttm", "total_revenue_ttm")
    df["gross_margin_ttm"] = (
        1 - safe_div("operating_cost_ttm", "total_revenue_ttm")
    )

    # 现金流质量 —— 最重要的单项红旗指标
    df["cfo_to_ni"] = safe_div("n_cashflow_act_ttm", "n_income_ttm")
    df["fcf_ttm"] = df.get("free_cashflow_ttm")

    # 资产结构
    df["debt_ratio"] = safe_div("total_liab", "total_assets")
    df["goodwill_to_equity"] = safe_div("goodwill", "equity_attr_p")
    df["ar_to_revenue"] = safe_div("accounts_receiv", "total_revenue_ttm")
    df["inv_to_revenue"] = safe_div("inventories", "total_revenue_ttm")

    # 扩产强度 —— 周期行业（新能源）核心观察项
    df["capex_to_revenue"] = safe_div("c_pay_acq_const_ttm", "total_revenue_ttm")
    df["cip_to_fixed"] = safe_div("cip", "fix_assets")

    # 订单前瞻 —— 成长行业（AI）核心观察项
    df["contract_liab_to_revenue"] = safe_div("contract_liab", "total_revenue_ttm")
    df["rd_intensity"] = safe_div("rd_exp_ttm", "total_revenue_ttm")

    # 同比增速（与去年同期比，即滞后 4 期）
    for base, name in [("total_revenue_ttm", "revenue_yoy"),
                       ("n_income_attr_p_ttm", "profit_yoy"),
                       ("accounts_receiv", "ar_yoy")]:
        if base in df.columns:
            prev = df.groupby("code")[base].shift(4)
            df[name] = ((pd.to_numeric(df[base], errors="coerce")
                         / prev.where(prev.abs() > 1e-6)) - 1).astype("Float64")

    return df


def latest_snapshot(df: pd.DataFrame) -> pd.DataFrame:
    """每只股票取最新一期，用于横截面筛选。"""
    if df.empty:
        return df
    return (
        df.sort_values(["code", "report_period"])
        .groupby("code", as_index=False)
        .tail(1)
        .reset_index(drop=True)
    )
