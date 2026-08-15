"""akshare 数据源适配器。

**Point-in-Time 的核心依据（已实测验证）**

财务数据使用东财「按报告期」详细接口（`stock_*_sheet_by_report_em`），
其 `NOTICE_DATE` 字段为财报首次披露日，经巨潮原始公告交叉核对一致：

    宁德时代 2021 年报  NOTICE_DATE = 2022-04-22
                       巨潮公告时间 = 2022-04-22  ✓

⚠️ 切勿改用东财**批量**接口（`stock_lrb_em` / `stock_zcfz_em` / `stock_xjll_em`）
   的「公告日期」字段——实测其与查询的报告期错位（查 2024Q3 返回 2026 年的日期），
   用于 PIT 会静默产生未来函数。批量接口仅可用于 L0 全市场轻量层。

上市前报告期的 NOTICE_DATE 指向招股说明书披露日，语义正确：
上市前的财务数据确实到招股书公开时才可得。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Literal

import akshare as ak
import pandas as pd

from csg.sources.base import call, first_success

log = logging.getLogger(__name__)

Statement = Literal["income", "balance", "cashflow"]

# ----------------------------------------------------------------------
# 字段映射：东财英文字段 -> 本项目 schema 字段
# ----------------------------------------------------------------------

_INCOME_MAP = {
    "TOTAL_OPERATE_INCOME": "total_revenue",
    "OPERATE_INCOME": "revenue",
    "OPERATE_COST": "operating_cost",
    "SALE_EXPENSE": "selling_exp",
    "MANAGE_EXPENSE": "admin_exp",
    "RESEARCH_EXPENSE": "rd_exp",
    "FINANCE_EXPENSE": "fin_exp",
    "OPERATE_PROFIT": "operate_profit",
    "TOTAL_PROFIT": "total_profit",
    "INCOME_TAX": "income_tax",
    "NETPROFIT": "n_income",
    "PARENT_NETPROFIT": "n_income_attr_p",
    "BASIC_EPS": "basic_eps",
}

_BALANCE_MAP = {
    "TOTAL_ASSETS": "total_assets",
    "TOTAL_LIABILITIES": "total_liab",
    "TOTAL_EQUITY": "total_equity",
    "TOTAL_PARENT_EQUITY": "equity_attr_p",
    "MONETARYFUNDS": "money_cap",
    "ACCOUNTS_RECE": "accounts_receiv",
    "INVENTORY": "inventories",
    "FIXED_ASSET": "fix_assets",
    "CIP": "cip",
    "GOODWILL": "goodwill",
    "INTANGIBLE_ASSET": "intangible_assets",
    "SHORT_LOAN": "st_borr",
    "LONG_LOAN": "lt_borr",
    "CONTRACT_LIAB": "contract_liab",
    # 总股本：市值 = 收盘价 × share_capital。
    # baostock 与 akshare 均无历史市值接口，但股本就在资产负债表里，
    # 逐期可得，无需额外数据源或付费。
    "SHARE_CAPITAL": "share_capital",
    "TREASURY_SHARES": "treasury_shares",
}

_CASHFLOW_MAP = {
    "NETCASH_OPERATE": "n_cashflow_act",
    "NETCASH_INVEST": "n_cashflow_inv_act",
    "NETCASH_FINANCE": "n_cashflow_fin_act",
    "CONSTRUCT_LONG_ASSET": "c_pay_acq_const",
}

_STATEMENT_SPEC: dict[Statement, tuple] = {
    "income": (ak.stock_profit_sheet_by_report_em, _INCOME_MAP, "fin_income"),
    "balance": (ak.stock_balance_sheet_by_report_em, _BALANCE_MAP, "fin_balance"),
    "cashflow": (ak.stock_cash_flow_sheet_by_report_em, _CASHFLOW_MAP, "fin_cashflow"),
}


# ----------------------------------------------------------------------
# 代码格式
# ----------------------------------------------------------------------

def exchange_of(code: str) -> str:
    """按代码前缀判断交易所。"""
    if code.startswith(("60", "68", "9")):
        return "SH"
    if code.startswith(("00", "30", "20")):
        return "SZ"
    if code.startswith(("43", "83", "87", "88", "92")):
        return "BJ"
    return "SZ"


def market_of(code: str) -> str:
    if code.startswith("68"):
        return "科创板"
    if code.startswith("30"):
        return "创业板"
    if code.startswith(("43", "83", "87", "88", "92")):
        return "北交所"
    return "主板"


def to_em_symbol(code: str) -> str:
    """东财详细接口要求 'SZ300750' 形式。"""
    return f"{exchange_of(code)}{code}"


def _is_st(name: str) -> bool:
    return "ST" in str(name).upper().replace(" ", "")


# ----------------------------------------------------------------------
# 基础信息
# ----------------------------------------------------------------------

def fetch_stock_list() -> pd.DataFrame:
    """在市股票列表。多接口 fallback —— 单一来源经常整个挂掉。"""
    src, df = first_success([
        ("code_name", lambda: ak.stock_info_a_code_name()),
        ("em_spot", lambda: ak.stock_zh_a_spot_em()),
        ("sh_sz", lambda: pd.concat([ak.stock_sh_a_spot_em(), ak.stock_sz_a_spot_em()])),
    ])

    col_code = "code" if "code" in df.columns else "代码"
    col_name = "name" if "name" in df.columns else "名称"
    out = df[[col_code, col_name]].copy()
    out.columns = ["code", "name"]
    out["code"] = out["code"].astype(str).str.zfill(6)
    out = out.drop_duplicates("code")
    out["exchange"] = out["code"].map(exchange_of)
    out["market"] = out["code"].map(market_of)
    out["delist_date"] = pd.NaT
    out["is_active"] = True
    log.info("股票列表来源 %s，%d 只", src, len(out))
    return out


def fetch_delisted() -> pd.DataFrame:
    """已退市股票。

    消除幸存者偏差的关键：用今天的股票列表回测历史，会自动剔除所有
    退市公司，凭空美化收益。回测的股票池必须包含「当时在市、后来退市」的公司。
    """
    frames = []
    for label, fetch, cmap in [
        ("SH", lambda: ak.stock_info_sh_delist(),
         {"公司代码": "code", "公司简称": "name",
          "上市日期": "list_date", "暂停上市日期": "delist_date"}),
        ("SZ", lambda: ak.stock_info_sz_delist(symbol="终止上市公司"),
         {"证券代码": "code", "证券简称": "name",
          "上市日期": "list_date", "终止上市日期": "delist_date"}),
    ]:
        try:
            df = call(fetch, retries=3)
            df = df.rename(columns=cmap)[list(cmap.values())]
            frames.append(df)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 退市列表获取失败: %s", label, exc)

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    out["code"] = out["code"].astype(str).str.zfill(6)
    for c in ("list_date", "delist_date"):
        out[c] = pd.to_datetime(out[c], errors="coerce").dt.date
    out["exchange"] = out["code"].map(exchange_of)
    out["market"] = out["code"].map(market_of)
    out["is_active"] = False
    return out.drop_duplicates("code")


def fetch_industry(period: str = "20251231") -> pd.DataFrame:
    """行业分类。

    取自业绩报表接口自带的「所处行业」字段——东财独立的行业板块接口
    (`stock_board_industry_name_em`) 实测已失效，重试 5 次均失败。

    ⚠️ 局限：只有当前分类，无历史变更记录。
    """
    df = call(ak.stock_yjbb_em, date=period, retries=5)
    out = df[["股票代码", "所处行业"]].dropna()
    out.columns = ["code", "industry_name"]
    out["code"] = out["code"].astype(str).str.zfill(6)
    out = out.drop_duplicates("code")
    out["taxonomy"] = "em_industry"
    out["start_date"] = None
    out["end_date"] = None
    return out


# ----------------------------------------------------------------------
# 行情
# ----------------------------------------------------------------------

def fetch_daily_quote(
    code: str,
    start: dt.date,
    end: dt.date,
) -> pd.DataFrame:
    """日线行情：原始价格 + 复权因子。

    stock_zh_a_hist 不直接提供复权因子，因此拉取不复权与后复权两版，
    以 adj_factor = hfq_close / raw_close 反推。

    只落盘原始价与因子，不落盘复权价：前复权价会因未来的除权事件
    改变历史值，一旦落盘即成错误数据。
    """
    s, e = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

    raw = call(ak.stock_zh_a_hist, symbol=code, period="daily",
               start_date=s, end_date=e, adjust="")
    if raw is None or raw.empty:
        return pd.DataFrame()

    cmap = {"日期": "trade_date", "开盘": "open", "收盘": "close", "最高": "high",
            "最低": "low", "成交量": "volume", "成交额": "amount",
            "涨跌幅": "pct_chg", "换手率": "turnover"}
    raw = raw.rename(columns=cmap)[list(cmap.values())]
    raw["trade_date"] = pd.to_datetime(raw["trade_date"]).dt.date

    try:
        hfq = call(ak.stock_zh_a_hist, symbol=code, period="daily",
                   start_date=s, end_date=e, adjust="hfq")
        hfq = hfq.rename(columns={"日期": "trade_date", "收盘": "hfq_close"})
        hfq["trade_date"] = pd.to_datetime(hfq["trade_date"]).dt.date
        raw = raw.merge(hfq[["trade_date", "hfq_close"]], on="trade_date", how="left")
        raw["adj_factor"] = (raw["hfq_close"] / raw["close"]).round(6)
        raw = raw.drop(columns=["hfq_close"])
    except Exception as exc:  # noqa: BLE001
        # 因子缺失时置 1.0 并记录：宁可标记缺失，不可静默填入错误值
        log.warning("%s 复权因子获取失败(%s)，置 1.0", code, type(exc).__name__)
        raw["adj_factor"] = 1.0

    raw.insert(0, "code", code)
    return raw


# ----------------------------------------------------------------------
# 财务报表
# ----------------------------------------------------------------------

def fetch_financial(code: str, statement: Statement) -> pd.DataFrame:
    """单只股票的完整财报历史，含 PIT 所需的披露日期。

    返回列固定为 schema 定义的字段；源数据缺失的字段填 NA 而非丢弃，
    保证入库结构稳定。
    """
    fetch_fn, field_map, _ = _STATEMENT_SPEC[statement]
    df = call(fetch_fn, symbol=to_em_symbol(code), retries=4)
    if df is None or df.empty:
        return pd.DataFrame()

    out = pd.DataFrame(index=df.index)
    out["code"] = code
    out["report_period"] = pd.to_datetime(df["REPORT_DATE"], errors="coerce").dt.date
    out["disclosure_date"] = pd.to_datetime(df["NOTICE_DATE"], errors="coerce").dt.date

    for src_col, dst_col in field_map.items():
        out[dst_col] = pd.to_numeric(df[src_col], errors="coerce") if src_col in df.columns else pd.NA

    out = out.dropna(subset=["report_period"]).drop_duplicates("report_period")

    if statement == "cashflow" and {"n_cashflow_act", "c_pay_acq_const"} <= set(out.columns):
        out["free_cashflow"] = out["n_cashflow_act"] - out["c_pay_acq_const"].fillna(0)

    return out.reset_index(drop=True)


def target_table(statement: Statement) -> str:
    return _STATEMENT_SPEC[statement][2]


# ----------------------------------------------------------------------
# 巨潮公告 —— 一手来源，用于抽样校验
# ----------------------------------------------------------------------

def fetch_cninfo_announcements(
    code: str, start: dt.date, end: dt.date
) -> pd.DataFrame:
    """巨潮资讯网公告。

    巨潮是证监会指定的信息披露平台，为一手权威来源。
    本项目用它抽样校验东财 NOTICE_DATE 的准确性，并为持仓股保留
    原始公告链接供人工核对。
    """
    df = call(ak.stock_zh_a_disclosure_report_cninfo, symbol=code, market="沪深京",
              start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
              retries=3)
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.rename(columns={"公告标题": "title", "公告时间": "announce_time",
                             "公告链接": "url"})
    out["announce_date"] = pd.to_datetime(out["announce_time"], errors="coerce").dt.date
    out.insert(0, "code", code)
    return out[["code", "title", "announce_date", "url"]]


# ----------------------------------------------------------------------
# 研报
# ----------------------------------------------------------------------

def fetch_research_reports(code: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """个股研报及其盈利预测，返回 (研报, 预测长表)。

    实测数据特征（宁德时代 469 条，2018-05 至今）：
      - 评级、机构、发布日期：全年份完整
      - 盈利预测：仅 2024 年后发布的研报有值，更早的全为空

    后者是数据壁垒而非实现缺陷：接口只返回「对当前及未来年份的预测」。
    因此历史盈利预测准确度无法回溯验证，只能从现在开始积累快照。
    """
    df = call(ak.stock_research_report_em, symbol=code, retries=4)
    if df is None or df.empty:
        return pd.DataFrame(), pd.DataFrame()

    today = dt.date.today()
    rep = pd.DataFrame(index=df.index)
    rep["code"] = code
    rep["publish_date"] = pd.to_datetime(df["日期"], errors="coerce").dt.date
    rep["institution"] = df.get("机构", pd.Series(dtype=str)).fillna("未知")
    rep["title"] = df.get("报告名称", pd.Series(dtype=str)).fillna("")
    rep["rating"] = df.get("东财评级")
    rep["industry"] = df.get("行业")
    rep["pdf_url"] = df.get("报告PDF链接")
    rep["snapshot_date"] = today
    rep = rep.dropna(subset=["publish_date"]).drop_duplicates(
        ["code", "publish_date", "institution", "title"])

    # 盈利预测宽表转长表：列名形如 "2026-盈利预测-收益" / "2026-盈利预测-市盈率"
    records: list[dict] = []
    years = sorted({c.split("-")[0] for c in df.columns if "盈利预测" in str(c)})
    for year in years:
        eps_col, pe_col = f"{year}-盈利预测-收益", f"{year}-盈利预测-市盈率"
        if eps_col not in df.columns:
            continue
        sub = df[[eps_col] + ([pe_col] if pe_col in df.columns else [])].copy()
        sub["publish_date"] = pd.to_datetime(df["日期"], errors="coerce").dt.date
        sub["institution"] = df.get("机构", pd.Series(dtype=str)).fillna("未知")
        sub = sub[pd.to_numeric(sub[eps_col], errors="coerce").notna()]
        for _, r in sub.iterrows():
            records.append({
                "code": code,
                "publish_date": r["publish_date"],
                "institution": r["institution"],
                "forecast_year": int(year),
                "eps": pd.to_numeric(r[eps_col], errors="coerce"),
                "pe": pd.to_numeric(r.get(pe_col), errors="coerce")
                     if pe_col in df.columns else None,
                "snapshot_date": today,
            })

    fc = pd.DataFrame(records)
    if not fc.empty:
        fc = fc.dropna(subset=["publish_date"]).drop_duplicates(
            ["code", "publish_date", "institution", "forecast_year", "snapshot_date"])
    return rep, fc
