"""证伪条件的自动核对。

**分层原则**：能用数据判定的自动判定并给出依据，判定不了的明确标注
「需人工」——**绝不猜测**。

猜一个看似合理的结论，比诚实说「我不知道」危险得多：前者会让人
误以为系统已经核对过了，从而跳过真正需要人看的部分。

可自动判定的条件形如：
    净利润同比增速连续两年低于15%
    毛利率跌破25%
    资产负债率超过70%

判定不了的（需分部数据或第三方行业数据）：
    储能业务收入增速连续两期低于30%   ← 分部数据未采集
    全球储能市占率出现下滑            ← 公开源无此数据
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import pandas as pd

Status = Literal["triggered", "not_triggered", "needs_human"]


@dataclass(frozen=True)
class CheckResult:
    condition: str
    status: Status
    evidence: str


# 指标关键词 → PIT 面板中的字段。
# 只收录**全公司口径**指标；分部数据（如储能业务、海外收入）
# 当前未采集，相关条件一律走 needs_human。
_METRIC_PATTERNS: list[tuple[str, str, bool]] = [
    # (关键词正则, 字段名, 是否百分比)
    (r"净利润?同比|归母净利.*同比|利润增速", "profit_yoy", True),
    (r"营收同比|营业收入.*同比|收入增速", "revenue_yoy", True),
    (r"毛利率", "gross_margin_ttm", True),
    (r"净利率", "net_margin_ttm", True),
    (r"资产负债率|负债率", "debt_ratio", True),
    (r"ROE|净资产收益率", "roe_ttm", True),
    (r"经营现金流.*净利|现金流.*净利润", "cfo_to_ni", False),
    (r"商誉", "goodwill_to_equity", True),
    (r"资本开支|资本支出|capex", "capex_to_revenue", True),
    (r"应收", "ar_to_revenue", True),
    (r"存货", "inv_to_revenue", True),
]

# 复合条件的语言标志。出现即需人工——
# 「A 超过 X **而** B 未改善」这类条件包含两个判据与一层因果关系，
# 正则无法可靠拆解，强行判定会给出看似成立实则错误的结论。
_COMPOUND_MARKERS = re.compile(r"而|且|并且|同时|以及|但")

# 分部/行业类关键词——出现即判定为需人工，不尝试用全公司指标近似。
# 用「全公司毛利率」代替「海外毛利率」会得出错误结论，
# 而错误的自动结论比没有结论更糟。
_SEGMENT_KEYWORDS = re.compile(
    r"储能业务|海外|分部|市占率|份额|细分|某项业务|订单|产能利用率")

_DIRECTION = [
    (re.compile(r"低于|跌破|下滑|小于|不足|低过"), "below"),
    (re.compile(r"高于|超过|大于|突破|超出"), "above"),
]


def _extract_threshold(text: str) -> float | None:
    """从条件文本中抽取阈值。支持 15%、0.15、15 三种写法。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if m:
        return float(m.group(1)) / 100
    m = re.search(r"(\d+\.\d+)", text)
    if m:
        return float(m.group(1))
    return None


def _consecutive_periods(text: str) -> int:
    """抽取「连续 N 期/年/季」中的 N，缺省为 1。"""
    m = re.search(r"连续\s*(两|三|四|\d+)\s*(期|年|季|个季度)", text)
    if not m:
        return 1
    word = m.group(1)
    return {"两": 2, "三": 3, "四": 4}.get(word, int(word) if word.isdigit() else 1)


def check_condition(condition: str, panel: pd.DataFrame) -> CheckResult:
    """核对单条证伪条件。

    `panel` 为该股票的 PIT 指标面板（按 report_period 升序）。
    """
    text = condition.strip()
    if not text:
        return CheckResult(condition, "needs_human", "条件为空")

    # 分部/行业类条件：数据不在库中，明确标注而非用全公司数据近似
    if _SEGMENT_KEYWORDS.search(text):
        return CheckResult(
            text, "needs_human",
            "涉及分部或行业数据（当前未采集），需查阅财报分部披露或行业报告")

    if _COMPOUND_MARKERS.search(text):
        return CheckResult(
            text, "needs_human",
            "复合条件（含多个判据或因果关系），需人工拆解后判断")

    matched = [f for pat, f, _ in _METRIC_PATTERNS if re.search(pat, text)]
    # 复合条件（含多个指标）不做自动判定。
    # 例如「资本开支超35%**而毛利率未改善**」同时命中两个指标，
    # 任取其一都会得出错误结论——错误的自动结论比没有结论更糟。
    if len(matched) > 1:
        return CheckResult(
            text, "needs_human",
            f"条件涉及多个指标（{'、'.join(matched)}），复合判定需人工")
    field = matched[0] if matched else None
    if field is None or field not in panel.columns:
        return CheckResult(text, "needs_human", "未能识别对应指标")

    threshold = _extract_threshold(text)
    if threshold is None:
        return CheckResult(text, "needs_human", "未能识别阈值")

    direction = next(
        (d for pat, d in _DIRECTION if pat.search(text)), None)
    if direction is None:
        return CheckResult(text, "needs_human", "未能识别比较方向")

    n = _consecutive_periods(text)
    series = pd.to_numeric(panel[field], errors="coerce").dropna()
    if len(series) < n:
        return CheckResult(
            text, "needs_human",
            f"可用数据仅 {len(series)} 期，不足以判定「连续 {n} 期」")

    recent = series.tail(n)
    hit = (recent < threshold).all() if direction == "below" else (recent > threshold).all()

    vals = "、".join(f"{v:.1%}" for v in recent) if abs(threshold) < 1 else \
           "、".join(f"{v:.2f}" for v in recent)
    op = "低于" if direction == "below" else "高于"
    thr = f"{threshold:.1%}" if abs(threshold) < 1 else f"{threshold:.2f}"

    return CheckResult(
        text,
        "triggered" if hit else "not_triggered",
        f"最近 {n} 期实际值 {vals}，阈值 {op} {thr}"
        f"{'（已触发）' if hit else '（未触发）'}")


def check_all(falsification: str, panel: pd.DataFrame) -> list[CheckResult]:
    """逐条核对证伪条件（以「；」分隔）。"""
    conditions = [c.strip() for c in str(falsification).split("；") if c.strip()]
    return [check_condition(c, panel) for c in conditions]


def summarize(results: list[CheckResult]) -> dict:
    """核对结果汇总，供推送与展示。"""
    return {
        "total": len(results),
        "triggered": sum(r.status == "triggered" for r in results),
        "not_triggered": sum(r.status == "not_triggered" for r in results),
        "needs_human": sum(r.status == "needs_human" for r in results),
    }
