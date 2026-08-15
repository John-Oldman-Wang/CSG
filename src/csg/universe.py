"""股票池解析。

分层依据见 docs/METHODOLOGY.md 第二节：
    L0 全市场 → 仅轻量数据，用于行业分位/市场水位/超额跌幅
    L1 基础池 → 本模块解析 config/universe.yaml 得到
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import yaml

from csg.storage import Database

CONFIG_PATH = Path("config/universe.yaml")


def load_config(path: Path | str = CONFIG_PATH) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def target_industries(cfg: dict | None = None) -> dict[str, list[str]]:
    """返回 {主题: [行业名]}，按配置决定是否含扩展池。"""
    cfg = cfg or load_config()
    core = cfg.get("core", {})
    result = {theme: list(names) for theme, names in core.items()}

    ext = cfg.get("extended", {})
    if ext.get("enabled"):
        for theme, names in ext.items():
            if theme == "enabled":
                continue
            result.setdefault(theme, []).extend(names)
    return result


def resolve_universe(
    db: Database,
    cfg: dict | None = None,
    *,
    as_of: dt.date | None = None,
) -> pd.DataFrame:
    """解析 L1 基础池。

    `as_of` 给定时按当时在市的股票过滤（含此后退市的公司），
    保证回测的股票池时点正确 —— 用今天的列表回测历史会产生幸存者偏差。
    """
    cfg = cfg or load_config()
    themes = target_industries(cfg)
    all_industries = sorted({n for names in themes.values() for n in names})
    if not all_industries:
        return pd.DataFrame(columns=["code", "name", "industry_name", "theme"])

    placeholders = ", ".join("?" * len(all_industries))
    sql = f"""
        SELECT b.code, b.name, b.market, b.list_date, b.delist_date,
               i.industry_name
        FROM stock_basic b
        JOIN industry_member i ON i.code = b.code
        WHERE i.taxonomy = 'em_industry'
          AND i.industry_name IN ({placeholders})
    """
    params: list = list(all_industries)

    if as_of is not None:
        sql += """
          AND (b.list_date IS NULL OR b.list_date <= ?)
          AND (b.delist_date IS NULL OR b.delist_date > ?)
        """
        params += [as_of, as_of]

    df = db.query(sql, params)
    if df.empty:
        return df

    ind_to_theme = {n: theme for theme, names in themes.items() for n in names}
    df["theme"] = df["industry_name"].map(ind_to_theme)

    rules = (cfg.get("circle_of_competence") or {}).get("exclude_rules") or {}
    if rules.get("exclude_st"):
        df = df[~df["name"].str.upper().str.replace(" ", "").str.contains("ST", na=False)]

    return df.drop_duplicates("code").reset_index(drop=True)
