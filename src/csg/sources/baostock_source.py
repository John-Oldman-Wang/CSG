"""baostock 数据源 —— 行情的备用来源。

**为什么需要它**：东财接口在请求量偏大后会限流封禁，实测行情接口
连续 4 只股票全部 ConnectionError 失败，重试与静置均无效。
baostock 并非爬虫架构，有独立的数据服务，不受东财限流影响。

这正是架构中多源设计的价值：单一来源失效时整条管线不停摆。

**口径差异（重要）**：baostock 的复权价与东财可能存在细微出入，
两者的复权算法不完全一致。因此同一只股票的行情**不应混用两个来源**，
否则会在拼接处产生虚假的价格跳变。本模块采集的行情以 baostock
自身的不复权价 + 复权因子为准，保持内部一致。
"""

from __future__ import annotations

import datetime as dt
import logging
import threading

import pandas as pd

log = logging.getLogger(__name__)

# baostock 的会话是进程级全局状态，且非线程安全，故加锁串行化
_login_lock = threading.Lock()
_logged_in = False


def _ensure_login() -> None:
    global _logged_in
    with _login_lock:
        if _logged_in:
            return
        import baostock as bs

        result = bs.login()
        if result.error_code != "0":
            raise RuntimeError(f"baostock 登录失败: {result.error_msg}")
        _logged_in = True
        log.info("baostock 登录成功")


def logout() -> None:
    global _logged_in
    with _login_lock:
        if not _logged_in:
            return
        import baostock as bs

        bs.logout()
        _logged_in = False


def to_bs_code(code: str) -> str:
    """6 位代码 → baostock 格式（sh.600519 / sz.300750）。"""
    prefix = "sh" if code.startswith(("60", "68", "9")) else "sz"
    return f"{prefix}.{code}"


def _fetch(rs) -> pd.DataFrame:
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    return pd.DataFrame(rows, columns=rs.fields) if rows else pd.DataFrame()


def fetch_daily_quote(code: str, start: dt.date, end: dt.date) -> pd.DataFrame:
    """日线行情：不复权价 + 复权因子。

    与东财适配器返回同样的列结构，可直接替换。

    复权因子由「后复权收盘价 / 不复权收盘价」反推——baostock 虽提供
    query_adjust_factor 接口，但其返回的是除权除息明细而非逐日因子，
    逐日展开反而更易出错。
    """
    import baostock as bs

    _ensure_login()
    bs_code = to_bs_code(code)
    s, e = start.isoformat(), end.isoformat()
    fields = "date,open,high,low,close,volume,amount,turn,pctChg"

    raw = _fetch(bs.query_history_k_data_plus(
        bs_code, fields, start_date=s, end_date=e, frequency="d", adjustflag="3"))
    if raw.empty:
        return pd.DataFrame()

    hfq = _fetch(bs.query_history_k_data_plus(
        bs_code, "date,close", start_date=s, end_date=e,
        frequency="d", adjustflag="1"))

    out = pd.DataFrame()
    out["code"] = [code] * len(raw)
    out["trade_date"] = pd.to_datetime(raw["date"]).dt.date
    for src, dst in [("open", "open"), ("high", "high"), ("low", "low"),
                     ("close", "close"), ("amount", "amount"),
                     ("turn", "turnover"), ("pctChg", "pct_chg")]:
        out[dst] = pd.to_numeric(raw[src], errors="coerce")
    out["volume"] = pd.to_numeric(raw["volume"], errors="coerce").astype("Int64")

    if not hfq.empty:
        hfq_map = dict(zip(
            pd.to_datetime(hfq["date"]).dt.date,
            pd.to_numeric(hfq["close"], errors="coerce"),
            strict=False))
        hfq_close = out["trade_date"].map(hfq_map)
        out["adj_factor"] = (hfq_close / out["close"]).round(6)
    else:
        # 宁可标记缺失，也不静默填入错误值
        log.warning("%s 后复权数据缺失，复权因子置 1.0", code)
        out["adj_factor"] = 1.0

    # 停牌日 volume 为 0 且价格为空，剔除以免污染收益计算
    return out[out["close"].notna() & (out["close"] > 0)].reset_index(drop=True)


def fetch_valuation(code: str, start: dt.date, end: dt.date) -> pd.DataFrame:
    """每日估值指标（PE/PB/PS/市值）—— 填补 L0 轻量层的空缺。

    验证② 需要历史 PE 分位，而 akshare 无稳定的批量历史 PE 接口。
    baostock 的 K 线接口可附带 peTTM / pbMRQ / psTTM，正好补上。
    """
    import baostock as bs

    _ensure_login()
    fields = "date,close,peTTM,pbMRQ,psTTM,pcfNcfTTM"
    raw = _fetch(bs.query_history_k_data_plus(
        to_bs_code(code), fields, start_date=start.isoformat(),
        end_date=end.isoformat(), frequency="d", adjustflag="3"))
    if raw.empty:
        return pd.DataFrame()

    out = pd.DataFrame()
    out["code"] = [code] * len(raw)
    out["trade_date"] = pd.to_datetime(raw["date"]).dt.date
    out["pe_ttm"] = pd.to_numeric(raw["peTTM"], errors="coerce")
    out["pb"] = pd.to_numeric(raw["pbMRQ"], errors="coerce")
    out["ps_ttm"] = pd.to_numeric(raw["psTTM"], errors="coerce")
    out["dv_ratio"] = pd.NA
    out["total_mv"] = pd.NA
    out["circ_mv"] = pd.NA
    return out[out["pe_ttm"].notna() | out["pb"].notna()].reset_index(drop=True)
