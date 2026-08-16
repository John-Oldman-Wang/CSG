"""组合约束检查（L5 决策层）。

**本模块不建议你买多少，只告诉你「这笔交易是否违反了你自己定的规则」。**

设计依据（见 METHODOLOGY ④）：机构投资者最大的制度优势不是选股方法，
而是把「不能做什么」写进风控系统——基金经理情绪上头时，系统会拒绝下单。
个人投资者最大的劣势不是信息，是没有任何东西拦得住自己。

因此本模块的输出是**违规清单**，不是建议。它可以被无视（这是你的钱），
但每次无视都会被记录，供复盘时统计「破戒次数」。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pandas as pd
import yaml

CONFIG_PATH = Path("config/position.yaml")

Severity = Literal["block", "warn"]


@dataclass(frozen=True)
class Violation:
    """一条约束违反。

    `block` 表示明确违反已定规则；`warn` 表示接近边界或需人工确认。
    二者都不阻止你交易——系统没有下单权限，也不该有。
    """

    rule: str
    severity: Severity
    message: str
    current: float | None = None
    limit: float | None = None


def load_config(path: Path | str = CONFIG_PATH) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


@dataclass
class Holding:
    code: str
    name: str
    weight: float          # 占总资产比例
    industry: str = ""
    theme: str = ""


def check_new_position(
    holdings: list[Holding],
    new_code: str,
    new_weight: float,
    *,
    industry: str = "",
    theme: str = "",
    cash_ratio: float = 0.0,
    cfg: dict | None = None,
) -> list[Violation]:
    """检查新增（或加仓）某标的后是否违反约束。

    `holdings` 为当前持仓，`new_weight` 为**变动后**该标的的总权重。
    """
    cfg = cfg or load_config()
    v: list[Violation] = []

    p = cfg["portfolio"]
    s = cfg["single_position"]
    c = cfg["concentration"]

    # ---- 单票上限 -------------------------------------------------
    if new_weight > s["max_weight"]:
        v.append(Violation(
            "单票上限", "block",
            f"{new_code} 权重 {new_weight:.1%} 超过上限 {s['max_weight']:.1%}。"
            f"你对单家公司的判断正确率若为 70%（已属优秀），"
            f"过重仓位意味着 30% 的概率让全部资产承受该错误",
            new_weight, s["max_weight"]))

    existing = next((h for h in holdings if h.code == new_code), None)
    if existing is None and new_weight > s["initial_weight"]:
        v.append(Violation(
            "首次建仓上限", "warn",
            f"首次建仓 {new_weight:.1%} 超过 {s['initial_weight']:.1%}，"
            f"未留加仓空间",
            new_weight, s["initial_weight"]))

    # ---- 持仓只数 -------------------------------------------------
    future_count = len(holdings) + (0 if existing else 1)
    if future_count > p["max_count"]:
        v.append(Violation(
            "持仓只数", "block",
            f"将达 {future_count} 只，超过上限 {p['max_count']}。"
            f"超出后实际上无法维持对每只的跟踪深度",
            future_count, p["max_count"]))

    # ---- 行业与主题集中度 ----------------------------------------
    def _group_weight(key: str, value: str) -> float:
        total = sum(getattr(h, key) == value and h.weight or 0.0 for h in holdings)
        if existing is not None and getattr(existing, key) == value:
            total = total - existing.weight + new_weight
        elif value:
            total += new_weight
        return total

    if industry:
        iw = _group_weight("industry", industry)
        if iw > c["max_industry_weight"]:
            v.append(Violation(
                "单行业上限", "block",
                f"行业「{industry}」合计 {iw:.1%} 超过 {c['max_industry_weight']:.1%}。"
                f"选出多只好公司却同属一个行业，看着分散实为单一赌注",
                iw, c["max_industry_weight"]))

    if theme:
        tw = _group_weight("theme", theme)
        if tw > c["max_theme_weight"]:
            v.append(Violation(
                "单主题上限", "block",
                f"主题「{theme}」合计 {tw:.1%} 超过 {c['max_theme_weight']:.1%}",
                tw, c["max_theme_weight"]))

    # ---- 现金比例 -------------------------------------------------
    delta = new_weight - (existing.weight if existing else 0.0)
    future_cash = cash_ratio - delta
    if future_cash < p["min_cash_ratio"]:
        v.append(Violation(
            "最低现金比例", "warn",
            f"交易后现金降至 {future_cash:.1%}，低于 {p['min_cash_ratio']:.1%}。"
            f"留现金不是保守，是保留选择权——回测中最佳买点出现时，"
            f"满仓者只能旁观",
            future_cash, p["min_cash_ratio"]))

    return v


def check_add_position(
    code: str,
    last_review_verdict: str | None,
    days_since_last_add: int | None,
    step: float,
    *,
    cfg: dict | None = None,
) -> list[Violation]:
    """检查加仓是否符合事先约定。

    **加仓规则必须事先写死**，否则「越跌越买」与「摊薄成本自欺」
    在事后无法区分——两者的操作完全一样，区别只在当时的理由，
    而理由是可以事后编的。
    """
    cfg = cfg or load_config()
    a = cfg["single_position"]["add_position"]
    v: list[Violation] = []

    required = a.get("require_review_verdict")
    if required and last_review_verdict != required:
        v.append(Violation(
            "加仓前置条件", "block",
            f"{code} 加仓要求最近一次复核结论为「{required}」（假设未被动摇），"
            f"当前为「{last_review_verdict or '无复核记录'}」。"
            f"未经复核的加仓无法与摊薄成本区分"))

    if step > a["max_step"]:
        v.append(Violation(
            "单次加仓幅度", "block",
            f"加仓 {step:.1%} 超过单次上限 {a['max_step']:.1%}",
            step, a["max_step"]))

    if days_since_last_add is not None and days_since_last_add < a["min_interval_days"]:
        v.append(Violation(
            "加仓间隔", "block",
            f"距上次加仓仅 {days_since_last_add} 天，"
            f"少于 {a['min_interval_days']} 天。连续补仓多为情绪驱动",
            days_since_last_add, a["min_interval_days"]))

    return v


def portfolio_health(holdings: list[Holding], cash_ratio: float,
                     *, cfg: dict | None = None) -> pd.DataFrame:
    """当前组合对照约束的整体状况。"""
    cfg = cfg or load_config()
    p, c = cfg["portfolio"], cfg["concentration"]

    rows = [
        {"项目": "持仓只数", "当前": len(holdings),
         "约束": f"{p['min_count']}–{p['max_count']}",
         "状态": "✓" if p["min_count"] <= len(holdings) <= p["max_count"] else "✗"},
        {"项目": "现金比例", "当前": f"{cash_ratio:.1%}",
         "约束": f"≥ {p['min_cash_ratio']:.0%}",
         "状态": "✓" if cash_ratio >= p["min_cash_ratio"] else "✗"},
    ]

    if holdings:
        mx = max(holdings, key=lambda h: h.weight)
        limit = cfg["single_position"]["max_weight"]
        rows.append({"项目": f"最大单票（{mx.name or mx.code}）",
                     "当前": f"{mx.weight:.1%}", "约束": f"≤ {limit:.0%}",
                     "状态": "✓" if mx.weight <= limit else "✗"})

        by_ind: dict[str, float] = {}
        for h in holdings:
            if h.industry:
                by_ind[h.industry] = by_ind.get(h.industry, 0.0) + h.weight
        if by_ind:
            top_ind, w = max(by_ind.items(), key=lambda kv: kv[1])
            lim = c["max_industry_weight"]
            rows.append({"项目": f"最大行业（{top_ind}）", "当前": f"{w:.1%}",
                         "约束": f"≤ {lim:.0%}", "状态": "✓" if w <= lim else "✗"})

    return pd.DataFrame(rows)


# ======================================================================
# 复核结论的读取端
# ======================================================================
#
# 事故记录（2026-08-16）：`review_conclusion` 表此前**只写不读**。
# check_add_position() 声明了 last_review_verdict 参数，却没有任何代码
# 把库里的结论传进来——你认真填写的复核结论进了库就没有下文。
#
# 这类「写入端完整、读取端缺失」的断链不会报错，只表现为规则不生效：
# 你以为加仓前置条件在拦着你，其实调用方永远传的是 None。


def last_conclusion(db, code: str) -> dict | None:
    """该标的最近一次复核结论。无记录返回 None。"""
    df = db.query(
        """
        SELECT verdict, would_rebuy, reasoning, falsified_items,
               next_review_date, action_taken, concluded_at
        FROM review_conclusion
        WHERE code = ? ORDER BY concluded_at DESC LIMIT 1
        """, [code])
    return None if df.empty else df.iloc[0].to_dict()


def days_since_last_action(db, code: str, actions: tuple[str, ...] = ("add",)) -> int | None:
    """距上次指定动作的天数，用于加仓间隔约束。无记录返回 None。"""
    marks = ",".join("?" * len(actions))
    df = db.query(
        f"""SELECT max(concluded_at) AS t FROM review_conclusion
            WHERE code = ? AND action_taken IN ({marks})""",
        [code, *actions])
    t = df["t"].iloc[0]
    if t is None or pd.isna(t):
        return None
    return (dt.datetime.now() - pd.Timestamp(t).to_pydatetime()).days


def check_add_position_for(db, code: str, step: float, *, cfg: dict | None = None
                           ) -> list[Violation]:
    """加仓检查的**接线版本** —— 自动从库中取出前置条件。

    与 check_add_position() 的区别仅在于参数来源：那个是纯函数便于测试，
    这个负责把它接到真实数据上。业务调用一律用本函数，
    避免再次出现「参数没人传，规则静默失效」。
    """
    c = last_conclusion(db, code)
    return check_add_position(
        code,
        last_review_verdict=c["verdict"] if c else None,
        days_since_last_add=days_since_last_action(db, code),
        step=step,
        cfg=cfg,
    )


def stalled_reviews(db, *, cfg: dict | None = None) -> list[Violation]:
    """连续判定「信息不足」达到上限的标的。

    对应 config/position.yaml 的 `max_insufficient_reviews`——
    该配置此前**只有配置项、没有实现**。

    它防的是用「再看看」无限期回避决策：这是面对亏损标的时
    最常见、也最容易自我合理化的逃避形式，因为每一次单独看
    都显得谨慎克制。
    """
    cfg = cfg or load_config()
    limit = cfg["exit"]["max_insufficient_reviews"]

    df = db.query(
        """
        WITH ranked AS (
            SELECT code, verdict,
                   row_number() OVER (PARTITION BY code ORDER BY concluded_at DESC) AS rn
            FROM review_conclusion
        )
        SELECT code, count(*) AS n
        FROM ranked
        WHERE rn <= ? AND verdict = 'insufficient'
        GROUP BY code HAVING count(*) >= ?
        """, [limit, limit])

    return [
        Violation(
            "复核停滞", "warn",
            f"{r['code']} 最近 {limit} 次复核均为「信息不足」。"
            f"该补的信息若 {limit} 次仍未补上，说明它要么拿不到、"
            f"要么你并不真的打算去拿——继续挂起等于用谨慎的外形回避决策",
            float(r["n"]), float(limit))
        for _, r in df.iterrows()
    ]
