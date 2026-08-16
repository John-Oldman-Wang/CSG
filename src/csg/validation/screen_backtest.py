"""验证③ 筛选条件的历史表现。

⚠️ **本模块验证的是筛选层，不是我们的方法论。**

方法论的核心是人的判断——护城河评估、买入理由、证伪条件、
复核时判断情绪面还是价值面。这些在历史上没有记录，无法重现。
本回测只能检验「机械筛选规则」，那是整套方法论中价值最低的一层。

若本模块跑出漂亮的年化收益，**不构成对方法论的验证**。
把它当成方法论被证实，比不做回测更危险——它会在真金白银下注前
提供虚假信心。

已实现的偏差控制：
- 时点正确的股票池（含此后退市者），消除幸存者偏差
- 财务数据以 disclosure_date 过滤，消除未来函数
- 计入双边交易成本
- 后复权价计算收益

**仍然存在、无法消除的偏差**（结论中必须声明）：
- 行业分类只有当前快照，早期年份归属可能不符
- 退市股缺少财务数据，实际未能完全进入筛选池
- 涨跌停、停牌导致的无法成交未建模
- 参数是在同一段历史上选定的，存在过拟合风险
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd

from csg.analysis import flags, metrics
from csg.storage import Database


@dataclass
class ScreenRule:
    """筛选条件。

    每条为 (字段, 比较符, 阈值)，在 PIT 快照上求交集。
    """

    conditions: list[tuple[str, str, float]] = field(default_factory=list)
    max_flag_score: int | None = 3     # 红旗分值上限，None 表示不限制
    top_n: int | None = 20             # 最终持仓数量
    rank_by: str | None = "roe_ttm"    # 排序字段，取最大者
    rank_ascending: bool = False

    def apply(self, snap: pd.DataFrame) -> pd.DataFrame:
        df = snap
        for col, op, val in self.conditions:
            if col not in df.columns:
                continue
            series = pd.to_numeric(df[col], errors="coerce")
            mask = {
                ">": series > val, ">=": series >= val,
                "<": series < val, "<=": series <= val,
            }[op]
            df = df[mask.fillna(False)]

        if self.max_flag_score is not None and "flag_score" in df.columns:
            df = df[df["flag_score"] <= self.max_flag_score]

        if self.rank_by and self.rank_by in df.columns:
            df = df.sort_values(self.rank_by, ascending=self.rank_ascending)
        if self.top_n:
            df = df.head(self.top_n)
        return df


@dataclass
class BacktestConfig:
    start: dt.date = dt.date(2018, 1, 1)
    end: dt.date = dt.date(2025, 12, 31)
    rebalance_months: int = 6          # 财报驱动的策略无需高频调仓
    cost_bps: float = 30.0             # 双边成本：佣金+印花税+滑点，30bp 偏保守
    equal_weight: bool = True


def run_backtest(
    db: Database,
    rule: ScreenRule,
    cfg: BacktestConfig | None = None,
    *,
    flag_cfg: dict | None = None,
) -> tuple[pd.DataFrame, dict]:
    """执行 PIT 筛选回测，返回 (每期明细, 汇总统计)。"""
    cfg = cfg or BacktestConfig()
    flag_cfg = flag_cfg or flags.load_config()

    periods: list[dict] = []
    cursor = cfg.start

    while cursor < cfg.end:
        next_date = (pd.Timestamp(cursor)
                     + pd.DateOffset(months=cfg.rebalance_months)).date()
        next_date = min(next_date, cfg.end)

        # 时点正确的股票池：含当时在市、此后退市的公司
        pool = db.pit_universe(cursor, exclude_st=True)
        if pool.empty:
            cursor = next_date
            continue

        panel = metrics.load_pit_panel(db, cursor, codes=pool["code"].tolist())
        if panel.empty:
            cursor = next_date
            continue

        snap = metrics.latest_snapshot(
            flags.evaluate(metrics.compute_ratios(panel), flag_cfg)
        )
        selected = rule.apply(snap)

        if not selected.empty:
            rets = _period_returns(db, selected["code"].tolist(), cursor, next_date)
            gross = float(rets["ret"].mean()) if not rets.empty else 0.0
            net = gross - cfg.cost_bps / 10_000 * 2
            bench, bench_n = _benchmark_return(db, cursor, next_date)
            periods.append({
                "start": cursor,
                "end": next_date,
                "选中数": len(selected),
                "实际有行情数": len(rets),
                "净收益": round(net, 4),
                "基准": round(bench, 4),
                "超额": round(net - bench, 4),
                "基准样本": bench_n,
                "胜率": round(float((rets["ret"] > 0).mean()), 3)
                if not rets.empty else None,
                "codes": ",".join(selected["code"].head(10)),
            })

        cursor = next_date

    detail = pd.DataFrame(periods)
    return detail, _summarize(detail, cfg)


def _benchmark_return(
    db: Database, start: dt.date, end: dt.date
) -> tuple[float, int]:
    """基准：同期**时点股票池内全部个股**的等权收益中位数。

    **没有基准，收益率无法解读**——若同期整个市场涨 20%，
    那么 8% 的年化实际上是负贡献。

    为何不用指数：本项目未采集指数行情，且股票池限定在新能源+AI，
    用沪深300 对照会混入赛道差异。用池内全体作基准，
    衡量的是「筛选相对于随机持有该赛道」的增量——这正是筛选层
    应当被检验的东西。
    """
    pool = db.pit_universe(start)
    if pool.empty:
        return 0.0, 0
    rets = _period_returns(db, pool["code"].tolist(), start, end)
    if rets.empty:
        return 0.0, 0
    return float(rets["ret"].median()), len(rets)


def _period_returns(
    db: Database, codes: list[str], start: dt.date, end: dt.date
) -> pd.DataFrame:
    """持有区间的后复权收益。

    起点取 start 之后第一个交易日的收盘价 —— 财报披露日当天买入
    不现实，且会引入当日异动收益。
    """
    if not codes:
        return pd.DataFrame(columns=["code", "ret"])

    placeholders = ", ".join("?" * len(codes))
    return db.query(
        f"""
        WITH px AS (
            SELECT code, trade_date, close * adj_factor AS p
            FROM daily_quote
            WHERE code IN ({placeholders}) AND trade_date > ? AND trade_date <= ?
        ),
        bounds AS (
            SELECT code, min(trade_date) AS d0, max(trade_date) AS d1
            FROM px GROUP BY code HAVING count(*) >= 2
        )
        SELECT b.code, (p1.p / nullif(p0.p, 0)) - 1 AS ret
        FROM bounds b
        JOIN px p0 ON p0.code = b.code AND p0.trade_date = b.d0
        JOIN px p1 ON p1.code = b.code AND p1.trade_date = b.d1
        WHERE p0.p > 0
        """,
        [*codes, start, end],
    )


def _summarize(detail: pd.DataFrame, cfg: BacktestConfig) -> dict:
    if detail.empty:
        return {"说明": "无有效回测区间，请先完成行情与财务数据采集"}

    net = detail["净收益"].astype(float)
    bench = detail["基准"].astype(float)
    cumulative = float((1 + net).prod() - 1)
    bench_cum = float((1 + bench).prod() - 1)
    years = max((cfg.end - cfg.start).days / 365.25, 1e-9)
    annualized = (1 + cumulative) ** (1 / years) - 1
    bench_ann = (1 + bench_cum) ** (1 / years) - 1

    equity = (1 + net).cumprod()
    drawdown = float((equity / equity.cummax() - 1).min())
    bench_equity = (1 + bench).cumprod()
    bench_dd = float((bench_equity / bench_equity.cummax() - 1).min())

    return {
        "区间": f"{cfg.start} ~ {cfg.end}",
        "调仓次数": len(detail),
        "策略累计": round(cumulative, 4),
        "基准累计": round(bench_cum, 4),
        "策略年化": round(float(annualized), 4),
        "基准年化": round(float(bench_ann), 4),
        "年化超额": round(float(annualized - bench_ann), 4),
        "策略最大回撤": round(drawdown, 4),
        "基准最大回撤": round(bench_dd, 4),
        "跑赢基准期数": f"{int((net > bench).sum())}/{len(detail)}",
        "单期胜率": round(float((net > 0).mean()), 3),
        "平均每期选中": round(float(detail["选中数"].mean()), 1),
        "⚠️ 声明": "本结果仅验证机械筛选层，不构成对方法论的验证；"
                   "方法论中人的判断部分无法回测。"
                   "基准为同期池内全体个股等权中位数——"
                   "衡量的是筛选相对于随机持有该赛道的增量",
    }
