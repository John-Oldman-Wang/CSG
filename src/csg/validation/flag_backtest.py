"""验证① 红旗规则的历史有效性。

**要回答的问题**：在暴雷发生之前的每个时点，我们的红旗规则能否发现问题？
提前多久？代价（误报）有多大？

这是三个验证里结论最可信的一个，因为：
- 「暴雷」是客观事件，不依赖人的判断
- 严格 PIT：每个评估时点只使用当时已披露的财报
- 产出直接可用：调整 config/exclusion.yaml 的阈值

**与收益率回测的区别**：本验证不预测股价，只检验「异常信号能否被提前识别」。
避开一次暴雷的价值，通常大于多选中一只上涨股。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import pandas as pd

from csg.analysis import flags, metrics
from csg.storage import Database

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BlowupEvent:
    """一次暴雷事件。"""

    code: str
    event_date: dt.date       # 事件被市场知晓的日期（披露日，非报告期）
    event_type: str           # delisting / profit_collapse / revenue_collapse
    detail: str


def find_blowup_events(
    db: Database,
    *,
    start: dt.date = dt.date(2017, 1, 1),
    end: dt.date | None = None,
    profit_collapse_ratio: float = -0.10,
    revenue_collapse_yoy: float = -0.50,
) -> pd.DataFrame:
    """识别历史暴雷事件。

    三类定义，均以**披露日**为事件日期——投资者是在财报公布那天
    才知道业绩崩塌的，不是在报告期结束那天。

    1. 退市：最终结果，最无争议
    2. 利润崩塌：净利润转负且亏损额超过净资产的一定比例
    3. 营收崩塌：营收同比腰斩

    注：退市股通常缺少财务数据（数据源多不保留），
    因此实际样本以 2、3 类为主。这是数据可得性的限制，需在结论中说明。
    """
    end = end or dt.date.today()
    events: list[dict] = []

    delisted = db.query(
        """
        SELECT code, name, delist_date
        FROM stock_basic
        WHERE delist_date IS NOT NULL AND delist_date BETWEEN ? AND ?
        """,
        [start, end],
    )
    for _, r in delisted.iterrows():
        events.append({
            "code": r["code"], "event_date": r["delist_date"],
            "event_type": "delisting", "detail": f"{r['name']} 退市",
        })

    # 利润崩塌：亏损额相对净资产的比例
    profit = db.query(
        """
        SELECT i.code, i.report_period, i.disclosure_date,
               i.n_income_attr_p, b.equity_attr_p
        FROM fin_income i
        JOIN fin_balance b ON b.code = i.code AND b.report_period = i.report_period
        WHERE i.disclosure_date BETWEEN ? AND ?
          AND i.report_period = date_trunc('year', i.report_period) + INTERVAL 11 MONTH + INTERVAL 30 DAY
          AND i.n_income_attr_p < 0
          AND b.equity_attr_p > 0
          AND i.n_income_attr_p / b.equity_attr_p < ?
        """,
        [start, end, profit_collapse_ratio],
    )
    for _, r in profit.iterrows():
        ratio = r["n_income_attr_p"] / r["equity_attr_p"]
        events.append({
            "code": r["code"], "event_date": r["disclosure_date"],
            "event_type": "profit_collapse",
            "detail": f"{r['report_period']} 亏损占净资产 {ratio:.1%}",
        })

    # 营收崩塌：与去年同期比较（同报告期，年份 -1）
    revenue = db.query(
        """
        WITH yoy AS (
            SELECT cur.code, cur.report_period, cur.disclosure_date,
                   cur.total_revenue AS rev,
                   prev.total_revenue AS rev_prev
            FROM fin_income cur
            JOIN fin_income prev
              ON prev.code = cur.code
             AND prev.report_period = cur.report_period - INTERVAL 1 YEAR
            WHERE cur.disclosure_date BETWEEN ? AND ?
              AND prev.total_revenue > 0
        )
        SELECT * FROM yoy WHERE rev / rev_prev - 1 < ?
        """,
        [start, end, revenue_collapse_yoy],
    )
    for _, r in revenue.iterrows():
        drop = r["rev"] / r["rev_prev"] - 1
        events.append({
            "code": r["code"], "event_date": r["disclosure_date"],
            "event_type": "revenue_collapse",
            "detail": f"{r['report_period']} 营收同比 {drop:.1%}",
        })

    if not events:
        return pd.DataFrame(columns=["code", "event_date", "event_type", "detail"])

    df = pd.DataFrame(events)
    df["event_date"] = pd.to_datetime(df["event_date"]).dt.date
    # 同一公司多次命中只保留最早一次，避免重复计数放大样本
    return (df.sort_values("event_date")
              .drop_duplicates("code", keep="first")
              .reset_index(drop=True))


def backtest_early_warning(
    db: Database,
    events: pd.DataFrame,
    *,
    lookback_quarters: int = 8,
    cfg: dict | None = None,
) -> pd.DataFrame:
    """回溯每个事件之前各时点的红旗状态。

    评估时点取事件日之前的若干个季度末。每个时点严格 PIT：
    只使用该时点已披露的财报，因此结果可回答
    「站在当时，我能否发现问题」。
    """
    cfg = cfg or flags.load_config()
    rows: list[dict] = []

    for _, ev in events.iterrows():
        code, ev_date = ev["code"], ev["event_date"]

        for q in range(1, lookback_quarters + 1):
            as_of = ev_date - pd.DateOffset(months=3 * q)
            as_of = as_of.date() if hasattr(as_of, "date") else as_of

            try:
                panel = metrics.load_pit_panel(db, as_of, codes=[code], periods=12)
                if panel.empty:
                    continue
                snap = metrics.latest_snapshot(
                    flags.evaluate(metrics.compute_ratios(panel), cfg)
                )
                if snap.empty:
                    continue
                r = snap.iloc[0]
                rows.append({
                    "code": code,
                    "event_type": ev["event_type"],
                    "event_date": ev_date,
                    "as_of": as_of,
                    "days_before": (ev_date - as_of).days,
                    "visible_period": r["report_period"],
                    "flag_score": int(r["flag_score"]),
                    "flag_count": int(r["flag_count"]),
                    "flag_names": r["flag_names"],
                })
            except Exception as exc:  # noqa: BLE001
                log.debug("%s @ %s 评估失败: %s", code, as_of, exc)

    return pd.DataFrame(rows)


def summarize(warnings_df: pd.DataFrame, *, min_score: int = 3) -> dict:
    """汇总预警效果。

    `min_score` 为判定「发出预警」的分值门槛，对应 exclusion.yaml
    中 high 级规则命中一次。
    """
    if warnings_df.empty:
        return {"事件数": 0, "说明": "无有效样本，请先完成数据采集"}

    hit = warnings_df[warnings_df["flag_score"] >= min_score]
    total_events = warnings_df["code"].nunique()
    warned_events = hit["code"].nunique()

    lead = (hit.sort_values("days_before", ascending=False)
               .drop_duplicates("code")["days_before"])

    by_rule = {}
    for names in hit["flag_names"].dropna():
        for n in str(names).split(","):
            if n:
                by_rule[n] = by_rule.get(n, 0) + 1

    return {
        "事件数": total_events,
        "被预警事件数": warned_events,
        "预警覆盖率": round(warned_events / total_events, 3) if total_events else 0.0,
        "最早预警中位天数": int(lead.median()) if len(lead) else 0,
        "最早预警均值天数": int(lead.mean()) if len(lead) else 0,
        "各规则命中次数": dict(sorted(by_rule.items(), key=lambda x: -x[1])),
    }


def false_positive_rate(
    db: Database,
    as_of: dt.date,
    events: pd.DataFrame,
    *,
    codes: list[str],
    min_score: int = 3,
    horizon_days: int = 730,
) -> dict:
    """误报率：被预警但此后未发生暴雷的比例。

    没有这个数字，覆盖率是没有意义的——把所有股票都标红，
    覆盖率必然 100%。两者必须同时看。
    """
    panel = metrics.load_pit_panel(db, as_of, codes=codes)
    if panel.empty:
        return {"说明": "该时点无可见数据"}

    snap = metrics.latest_snapshot(flags.evaluate(metrics.compute_ratios(panel)))
    flagged = set(snap.loc[snap["flag_score"] >= min_score, "code"])
    if not flagged:
        return {"预警数": 0}

    horizon_end = as_of + dt.timedelta(days=horizon_days)
    actual = set(
        events.loc[
            (events["event_date"] > as_of) & (events["event_date"] <= horizon_end),
            "code",
        ]
    )
    true_pos = flagged & actual
    return {
        "评估时点": str(as_of),
        "观察窗口天数": horizon_days,
        "预警数": len(flagged),
        "其中真实暴雷": len(true_pos),
        "误报率": round(1 - len(true_pos) / len(flagged), 3),
    }
