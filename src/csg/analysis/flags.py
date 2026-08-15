"""财务红旗规则（L2 排除层）。

设计取向：**宁可漏报，不可制造虚假安全感。**

每条规则输出命中标记 + 触发时的实际数值，后者用于验证① 的阈值调优
以及人工复核时的解释。规则本身不下「这家公司有问题」的结论——
它只指出「这个数字不寻常，你需要去看」，判断仍归人（METHODOLOGY 原则 1）。

所有规则在 PIT 面板上计算，因此可回溯到历史任一时点，
回答「在 T 时刻我能否发现问题」——这正是验证① 的核心问题。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml

CONFIG_PATH = Path("config/exclusion.yaml")


@dataclass(frozen=True)
class FlagHit:
    """单条规则的评估结果。"""

    name: str
    severity: str
    hit: pd.Series          # bool，索引对齐输入面板
    detail: pd.Series       # 触发时的实际数值，供人工解释


def load_config(path: Path | str = CONFIG_PATH) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(pd.NA, index=df.index, dtype="Float64")
    return pd.to_numeric(df[col], errors="coerce").astype("Float64")


def _consecutive(cond: pd.Series, groups: pd.Series, periods: int) -> pd.Series:
    """连续 N 期均满足条件。

    单季波动不构成红旗——制造业的现金流有明显季节性，
    单看一期会产生大量误报。
    """
    if periods <= 1:
        return cond.fillna(False)
    rolled = (
        cond.fillna(False).astype(int)
        .groupby(groups)
        .rolling(periods, min_periods=periods)
        .sum()
        .reset_index(level=0, drop=True)
    )
    return (rolled >= periods).fillna(False)


# ----------------------------------------------------------------------
# 规则实现
# ----------------------------------------------------------------------

def flag_cfo_divergence(df: pd.DataFrame, cfg: dict) -> FlagHit:
    """经营现金流与净利润长期背离。

    利润可以通过收入确认时点、减值计提等方式调节，现金难以长期伪造。
    净利润为负时该比率无意义，一律排除（亏损本身另有信号覆盖）。
    """
    ratio = _num(df, "cfo_to_ni")
    ni = _num(df, "n_income_ttm")
    cond = (ratio < cfg["threshold"]) & (ni > 0)
    hit = _consecutive(cond, df["code"], cfg.get("consecutive_periods", 1))
    return FlagHit("cfo_divergence", cfg["severity"], hit, ratio)


def flag_ar_outpacing_revenue(df: pd.DataFrame, cfg: dict) -> FlagHit:
    """应收账款增速远超营收增速。

    典型的收入注水模式：账面确认了收入，钱没进来。
    加上应收占比下限，避免基数极小的公司产生大量噪音。
    """
    excess = _num(df, "ar_yoy") - _num(df, "revenue_yoy")
    weight = _num(df, "ar_to_revenue")
    hit = ((excess > cfg["excess_growth"]) &
           (weight > cfg["min_ar_to_revenue"])).fillna(False)
    return FlagHit("ar_outpacing_revenue", cfg["severity"], hit, excess)


def flag_goodwill_risk(df: pd.DataFrame, cfg: dict) -> FlagHit:
    """商誉占净资产比重过高。

    并购形成的商誉是利润的定时炸弹：一旦标的业绩不达预期，
    减值会在某个季度一次性冲击利润表。
    """
    ratio = _num(df, "goodwill_to_equity")
    hit = (ratio > cfg["goodwill_to_equity"]).fillna(False)
    return FlagHit("goodwill_risk", cfg["severity"], hit, ratio)


def flag_inventory_buildup(df: pd.DataFrame, cfg: dict) -> FlagHit:
    """存货占营收比重高且同比上升。

    周期行业（光伏、锂电）价格下行期的典型前兆：
    产品卖不动，存货堆积，随后计提大额跌价准备。
    """
    ratio = _num(df, "inv_to_revenue")
    prev = ratio.groupby(df["code"]).shift(4)
    yoy = (ratio / prev.where(prev.abs() > 1e-6)) - 1
    hit = ((ratio > cfg["inv_to_revenue"]) &
           (yoy > cfg["yoy_increase"])).fillna(False)
    return FlagHit("inventory_buildup", cfg["severity"], hit, ratio)


def flag_high_leverage(df: pd.DataFrame, cfg: dict) -> FlagHit:
    ratio = _num(df, "debt_ratio")
    hit = (ratio > cfg["debt_ratio"]).fillna(False)
    return FlagHit("high_leverage", cfg["severity"], hit, ratio)


def flag_aggressive_capex(df: pd.DataFrame, cfg: dict) -> FlagHit:
    """扩产激进。

    单独出现不构成问题——高增长期的扩产是合理的。
    危险在于「全行业同步扩产」，那是产能过剩与价格战的前兆，
    因此本规则须结合行业层数据解读，严重度设为 low。
    """
    capex = _num(df, "capex_to_revenue")
    cip = _num(df, "cip_to_fixed")
    hit = ((capex > cfg["capex_to_revenue"]) |
           (cip > cfg["cip_to_fixed"])).fillna(False)
    return FlagHit("aggressive_capex", cfg["severity"], hit, capex)


_RULES = {
    "cfo_divergence": flag_cfo_divergence,
    "ar_outpacing_revenue": flag_ar_outpacing_revenue,
    "goodwill_risk": flag_goodwill_risk,
    "inventory_buildup": flag_inventory_buildup,
    "high_leverage": flag_high_leverage,
    "aggressive_capex": flag_aggressive_capex,
}

_SEVERITY_WEIGHT = {"high": 3, "medium": 2, "low": 1}


def evaluate(df: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    """在 PIT 面板上评估全部启用的规则。

    返回原面板 + 每条规则的 `flag_*` 命中列、`flagval_*` 数值列，
    以及汇总的 `flag_count` 与按严重度加权的 `flag_score`。
    """
    if df.empty:
        return df

    cfg = cfg or load_config()
    rules_cfg = cfg.get("financial_flags", {})
    out = df.copy()
    hits, weights = [], []

    for name, rule_cfg in rules_cfg.items():
        if not rule_cfg.get("enabled", False) or name not in _RULES:
            continue
        result = _RULES[name](out, rule_cfg)
        out[f"flag_{name}"] = result.hit
        out[f"flagval_{name}"] = result.detail
        hits.append(f"flag_{name}")
        weights.append(_SEVERITY_WEIGHT.get(result.severity, 1))

    if hits:
        out["flag_count"] = out[hits].sum(axis=1)
        out["flag_score"] = sum(
            out[h].astype(int) * w for h, w in zip(hits, weights, strict=True)
        )
        out["flag_names"] = out[hits].apply(
            lambda r: ",".join(h.removeprefix("flag_") for h in hits if r[h]), axis=1
        )
    else:
        out["flag_count"] = 0
        out["flag_score"] = 0
        out["flag_names"] = ""

    return out
