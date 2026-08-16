#!/usr/bin/env python
"""机构档位分析 —— 研究实力更强的券商，研报预测力是否更强？

## 这个脚本要回答什么

补进同花顺后，样本第一次有了「层次」。此前东财样本全是中小券商，
看不出档位差异；现在可以问一个此前问不出的问题：

    档位更高的券商，其研报的事后表现是否系统性更好？

**这个问题的价值在于它能替我们回答一个拿不到数据的问题。**
中信、中金、国君、海通、中信建投在三个免费渠道均无（商业策略所致）。
但若「能观察到的档位差」本身不存在，那么再往上一档大概率也一样——
对拿不到的那部分的好奇，就可以放下了。

## 口径

- 收益：发布次日**开盘价**买入（开盘涨停顺延，连续涨停超 5 日放弃），
  持有 N 个交易日按收盘价卖出。与 api/main.py 的 ENTRY_CTE 同规则。
- 超额：个股收益 − **同期沪深300 收益**。绝对收益在牛市里人人为正，
  无法比较。
- 分期：发现期 2018-2022 / 验证期 2023-至今。**只采信两期同向的差异**——
  单期结果在机构维度上已被证明不可复现（秩相关 −0.366）。

## 档位划分

按券商综合实力粗分三档（依 2024 年营收与研究业务规模，作者判断，
非官方分类）。**这是本分析最主观的一步**，故把名单写死在此处并纳入 git，
调整须留 commit 记录，避免事后按结果反推分组。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# 一档：综合实力最强，研究定价能力最高。**三个免费渠道均无**，此处仅作占位，
#       用于说明「我们观察不到的是哪一档」。
TIER1 = {"中信证券", "中金公司", "国泰君安", "国泰海通", "海通证券", "中信建投"}

# 二档：头部与准头部。同花顺补进来的正是这一档，是本分析的核心增量。
TIER2 = {"华泰证券", "招商证券", "广发证券", "申万宏源", "东方证券",
         "光大证券", "中国银河", "兴业证券", "长江证券", "国信证券",
         "中泰证券", "瑞银证券", "国联民生"}

# 三档：中小券商。东财样本几乎全部落在这里。
def tier_of(inst: str) -> str:
    if inst in TIER1:
        return "T1 顶级（观察不到）"
    if inst in TIER2:
        return "T2 头部"
    return "T3 中小"


HORIZON = 20
IN_SAMPLE = ("2018-01-01", "2022-12-31")
OUT_SAMPLE = ("2023-01-01", "2026-12-31")


SQL = """
WITH q AS (
    SELECT code, trade_date, open, close, adj_factor,
           lag(close) OVER (PARTITION BY code ORDER BY trade_date) AS prev_close,
           row_number() OVER (PARTITION BY code ORDER BY trade_date) AS rn
    FROM daily_quote
    WHERE close IS NOT NULL AND open IS NOT NULL
      AND adj_factor IS NOT NULL AND close > 0
),
lim AS (
    SELECT *, CASE
        WHEN code LIKE '688%' THEN 0.20
        WHEN code LIKE '300%' AND trade_date >= DATE '2020-08-24' THEN 0.20
        WHEN code LIKE '8%' OR code LIKE '4%' THEN 0.30
        ELSE 0.10 END AS lim_pct
    FROM q
),
buyable AS (
    SELECT *, prev_close IS NOT NULL
           AND open < round(prev_close * (1 + lim_pct), 2) - 0.005 AS can_buy
    FROM lim
),
mx AS (SELECT code, max(rn) AS last_rn FROM q GROUP BY code),
cand AS (
    SELECT r.code, r.publish_date, r.institution, r.title, r.source,
           b.rn, b.trade_date, b.open, b.adj_factor, b.can_buy,
           row_number() OVER (
               PARTITION BY r.code, r.publish_date, r.institution, r.title, r.source
               ORDER BY b.trade_date) AS seq
    FROM research_report r
    JOIN buyable b ON b.code = r.code AND b.trade_date > r.publish_date
    WHERE r.rating IN ('买入', '增持')
),
picked AS (
    SELECT *, min(CASE WHEN can_buy THEN seq END) OVER (
        PARTITION BY code, publish_date, institution, title, source) AS ok_seq
    FROM cand WHERE seq <= 6
),
entry AS (
    SELECT code, publish_date, institution, source,
           rn AS ern, open * adj_factor AS buy_adj
    FROM picked WHERE seq = ok_seq
)
SELECT e.institution AS 机构, e.source AS 源, e.publish_date AS 发布日,
       (x.close * x.adj_factor / e.buy_adj) - 1 AS ret,
       (ix.close / ib.close) - 1 AS idx_ret
FROM entry e
JOIN mx ON mx.code = e.code
JOIN q x ON x.code = e.code AND x.rn = e.ern + ?
LEFT JOIN q eq ON eq.code = e.code AND eq.rn = e.ern
LEFT JOIN index_quote ib ON ib.code = '000300' AND ib.trade_date = eq.trade_date
LEFT JOIN index_quote ix ON ix.code = '000300' AND ix.trade_date = x.trade_date
WHERE e.ern + ? <= mx.last_rn
"""


def main() -> int:
    from csg.storage import Database

    db = Database(str(ROOT / "data/csg.duckdb"), read_only=True)
    d = db.query(SQL, [HORIZON, HORIZON])
    db.close()

    if d.empty:
        print("无数据")
        return 1

    d["idx_ret"] = d["idx_ret"].fillna(0.0)
    d["超额"] = d["ret"] - d["idx_ret"]
    d["档位"] = d["机构"].map(tier_of)
    d["发布日"] = pd.to_datetime(d["发布日"])

    def period(x: pd.Timestamp) -> str | None:
        if pd.Timestamp(IN_SAMPLE[0]) <= x <= pd.Timestamp(IN_SAMPLE[1]):
            return "发现期"
        if x >= pd.Timestamp(OUT_SAMPLE[0]):
            return "验证期"
        return None

    d["期"] = d["发布日"].map(period)
    d = d.dropna(subset=["期"])

    print(f"样本 {len(d):,} 笔（持有 {HORIZON} 交易日，仅买入/增持评级）")
    print(f"来源分布: {d['源'].value_counts().to_dict()}")
    print()

    print("=" * 78)
    print("① 各档位机构数与样本量")
    print("=" * 78)
    g = (d.groupby("档位")
           .agg(机构数=("机构", "nunique"), 样本=("ret", "size"))
           .reset_index())
    print(g.to_string(index=False))
    print()
    for t in sorted(d["档位"].unique()):
        insts = sorted(d.loc[d["档位"] == t, "机构"].unique())
        print(f"  {t}: {', '.join(insts[:16])}" + (" …" if len(insts) > 16 else ""))
    print()

    print("=" * 78)
    print("② 档位 × 分期 的超额收益（对沪深300 同窗口）")
    print("=" * 78)
    piv = (d.groupby(["档位", "期"])
             .agg(样本=("超额", "size"),
                  均值=("超额", "mean"),
                  中位=("超额", "median"),
                  胜率=("超额", lambda s: (s > 0).mean()))
             .reset_index())
    for _, r in piv.iterrows():
        print(f"  {r['档位']:14} {r['期']}  n={int(r['样本']):>6}  "
              f"均值 {r['均值']:>+7.3%}  中位 {r['中位']:>+7.3%}  胜率 {r['胜率']:>6.1%}")
    print()

    print("=" * 78)
    print("③ 判定：T2 头部 是否稳定优于 T3 中小")
    print("=" * 78)
    verdict = []
    for per in ("发现期", "验证期"):
        a = d[(d["档位"] == "T2 头部") & (d["期"] == per)]["超额"]
        b = d[(d["档位"] == "T3 中小") & (d["期"] == per)]["超额"]
        if len(a) < 100 or len(b) < 100:
            print(f"  {per}: 样本不足（T2 {len(a)} / T3 {len(b)}），跳过")
            continue
        diff = a.mean() - b.mean()
        # 独立样本的均值差 z 检验（大样本，正态近似即可，不引入 scipy）
        se = np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
        z = diff / se if se > 0 else 0.0
        verdict.append(diff)
        print(f"  {per}: T2 {a.mean():+.3%}（n={len(a)}） − "
              f"T3 {b.mean():+.3%}（n={len(b)}） = {diff:+.3%}   z={z:+.2f}")
    print()

    if len(verdict) == 2:
        same = verdict[0] * verdict[1] > 0
        if same and verdict[1] > 0:
            print("  ✓ 两期同向为正 —— 档位差异真实存在，"
                  "则观察不到的 T1 大概率更强，值得继续设法获取")
        elif same:
            print("  ✗ 两期同向为负 —— 头部反而更差（可能是覆盖标的更大市值、"
                  "波动更低所致），需再拆分市值后复核")
        else:
            print("  ✗ 两期符号相反 —— 典型噪音特征，与机构排名不可复现"
                  "（秩相关 −0.366）一致。\n"
                  "     ⇒ 连能观察到的档位差都不存在，对 T1 的好奇可以放下。")
    print()

    print("=" * 78)
    print("④ T2 头部各家明细（两期都要有 ≥50 样本才列出）")
    print("=" * 78)
    t2 = d[d["档位"] == "T2 头部"]
    rows = []
    for inst, gi in t2.groupby("机构"):
        a = gi[gi["期"] == "发现期"]["超额"]
        b = gi[gi["期"] == "验证期"]["超额"]
        if len(a) < 50 or len(b) < 50:
            continue
        rows.append({"机构": inst, "发现n": len(a), "发现超额": a.mean(),
                     "验证n": len(b), "验证超额": b.mean(),
                     "同向": "✓" if a.mean() * b.mean() > 0 else "✗"})
    if rows:
        t = pd.DataFrame(rows).sort_values("验证超额", ascending=False)
        for _, r in t.iterrows():
            print(f"  {r['机构']:8} 发现 {r['发现超额']:>+7.3%}（{int(r['发现n']):>4}）"
                  f"  验证 {r['验证超额']:>+7.3%}（{int(r['验证n']):>4}）  {r['同向']}")
        n_same = (t["同向"] == "✓").sum()
        print(f"\n  两期同向: {n_same}/{len(t)} 家"
              f"（随机基准约 {len(t) / 2:.0f}/{len(t)}）")
    else:
        print("  无机构满足两期各 ≥50 样本")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
