"""验证④ 研报是否具有预测力。

**要回答的问题**：研报的哪些内容真的能预判股价走势？

笼统地问「研报有没有用」无法检验，必须拆成可证伪的子问题。

---

## 数据边界（实测结论，直接决定能验什么）

**没有目标价字段。** 接口提供预测 EPS 与预测 PE，但实测同一时期
不同机构的 `EPS × PE` 乘积高度一致（宁德时代 2026-07：391.6 / 400.1 / 402.0），
若 PE 是各家给出的目标估值，乘积作为目标价理应显著分歧。
乘积如此接近只能说明：

    预测PE = 发布日股价 ÷ 预测EPS      即 EPS × PE ≈ 发布日股价

因此「是否涨到目标价」「最高价/目标价之比」**无法验证**。
目标价只存在于 PDF 正文，需下载解析十余万份文件，暂不实施。

**历史盈利预测不可回溯**：2018-2023 年发布的研报，其预测字段非空率为 0%。
接口只返回「对当前及未来年份的预测」。故预测准确度只能从 2024 年起验证。

---

## 两个容易被漏掉的维度

**① 路径** —— 最终涨到目标不等于你拿得住。
「12 个月涨 30% 但中途 −45%」与「涨 30% 最大回撤 −8%」终点收益相同，
实际可执行性天差地别。只看终点收益是回测最常见的自欺，
因此本模块计算持有期内的最大回撤、最大浮盈浮亏、达标耗时。

**② 基准** —— 绝对涨幅在牛市中毫无意义。
全部收益均转换为**超额收益**（减去同月全样本中位数），
否则会把大盘行情误判为研报有效。

---

## 过拟合防线（强制，非可选）

8 年数据里只要切法够多，总能找出「显著」结果。故样本分割写死：

    发现期 2018-01-01 ~ 2022-12-31
    验证期 2023-01-01 ~ 至今

**任何在验证期不成立的结论一律不予采信。** 事先定死，否则事后总能自圆其说。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from csg.storage import Database

IN_SAMPLE = (dt.date(2018, 1, 1), dt.date(2022, 12, 31))
OUT_SAMPLE = (dt.date(2023, 1, 1), dt.date(2026, 12, 31))

DEFAULT_HORIZONS = (20, 60, 120, 250)      # 交易日：约 1/3/6/12 个月
MAIN_HORIZON = 250                          # 路径指标的观察窗口，对应业内默认的 12 个月
GAIN_TARGETS = (0.10, 0.20, 0.30)           # 达标耗时的考察档位


def _outcome_sql(horizons: tuple[int, ...], main: int) -> str:
    """构造单条研报的完整结果指标 SQL。

    全部计算下推到 DuckDB：8 GB 内存下不允许把全量行情载入 pandas
    （见 ARCHITECTURE.md 4.1）。

    入场价取发布日**之后**首个交易日收盘价——发布当日买入不现实，
    且会混入公告日异动。
    """
    ret_cols = ",\n".join(
        f"           (h{h}.p / e.p) - 1 AS ret_{h}" for h in horizons
    )
    ret_joins = "\n".join(
        f"    LEFT JOIN px h{h} ON h{h}.code = ent.code "
        f"AND h{h}.rn = ent.entry_rn + {h}"
        for h in horizons
    )
    target_cols = ",\n".join(
        f"           min(CASE WHEN w.hi / w.entry_p - 1 >= {t} "
        f"THEN w.offset END) AS days_to_{int(t * 100)}pct"
        for t in GAIN_TARGETS
    )

    return f"""
    WITH px AS (
        SELECT code, trade_date,
               close * adj_factor AS p,
               high  * adj_factor AS hi,
               low   * adj_factor AS lo,
               row_number() OVER (PARTITION BY code ORDER BY trade_date) AS rn
        FROM daily_quote
        WHERE close IS NOT NULL AND adj_factor IS NOT NULL AND close > 0
    ),
    rpt AS (
        SELECT code, publish_date, institution, rating, title, industry
        FROM research_report
        WHERE rating IS NOT NULL AND rating <> ''
          AND publish_date BETWEEN ? AND ?
    ),
    ent AS (
        SELECT r.code, r.publish_date, r.institution, r.rating, r.title,
               any_value(r.industry) AS industry,
               min(px.rn) AS entry_rn
        FROM rpt r
        JOIN px ON px.code = r.code AND px.trade_date > r.publish_date
        GROUP BY r.code, r.publish_date, r.institution, r.rating, r.title
    ),
    base AS (
        SELECT ent.code, ent.publish_date, ent.institution, ent.rating, ent.title,
               ent.industry,
               ent.entry_rn, e.trade_date AS entry_date, e.p AS entry_p,
{ret_cols}
        FROM ent
        JOIN px e ON e.code = ent.code AND e.rn = ent.entry_rn
{ret_joins}
        WHERE e.p > 0
    ),
    -- 持有期内的逐日价格，用于路径指标
    win AS (
        SELECT b.code, b.publish_date, b.institution, b.title,
               b.entry_p, px.rn - b.entry_rn AS offset,
               px.p, px.hi, px.lo,
               max(px.p) OVER (
                   PARTITION BY b.code, b.publish_date, b.institution, b.title
                   ORDER BY px.rn ROWS UNBOUNDED PRECEDING
               ) AS running_max
        FROM base b
        JOIN px ON px.code = b.code
               AND px.rn BETWEEN b.entry_rn AND b.entry_rn + {main}
    ),
    path AS (
        SELECT w.code, w.publish_date, w.institution, w.title,
               max(w.hi) / max(w.entry_p) - 1        AS max_gain,
               min(w.lo) / max(w.entry_p) - 1        AS max_loss,
               min(w.p / nullif(w.running_max, 0)) - 1 AS max_drawdown,
{target_cols}
        FROM win w
        GROUP BY w.code, w.publish_date, w.institution, w.title
    ),
    -- 发布前 60 个交易日的走势：区分追涨型与逆势型研报
    prior AS (
        SELECT b.code, b.publish_date, b.institution, b.title,
               (b.entry_p / nullif(p0.p, 0)) - 1 AS prior_ret_60
        FROM base b
        LEFT JOIN px p0 ON p0.code = b.code AND p0.rn = b.entry_rn - 60
    ),
    -- 同期覆盖密度：前 30 天内该股票的研报数量
    density AS (
        SELECT r.code, r.publish_date, r.institution, r.title,
               (SELECT count(*) FROM research_report r2
                 WHERE r2.code = r.code
                   AND r2.publish_date <= r.publish_date
                   AND r2.publish_date > r.publish_date - INTERVAL 30 DAY
               ) AS coverage_30d
        FROM rpt r
    ),
    -- 该机构对该股票是否首次覆盖
    firstcov AS (
        SELECT code, institution, min(publish_date) AS first_date
        FROM research_report GROUP BY code, institution
    ),
    -- 距最近一次财报披露的天数：区分财报点评与独立深度报告
    afterreport AS (
        SELECT b.code, b.publish_date, b.institution, b.title,
               date_diff('day',
                   (SELECT max(d.disclosure_date) FROM disclosure_date d
                     WHERE d.code = b.code AND d.disclosure_date <= b.publish_date),
                   b.publish_date) AS days_since_report
        FROM base b
    )
    SELECT b.*,
           path.max_gain, path.max_loss, path.max_drawdown,
           {", ".join(f"path.days_to_{int(t*100)}pct" for t in GAIN_TARGETS)},
           prior.prior_ret_60,
           density.coverage_30d,
           (fc.first_date = b.publish_date) AS is_first_coverage,
           ar.days_since_report
    FROM base b
    LEFT JOIN path     ON path.code = b.code AND path.publish_date = b.publish_date
                      AND path.institution = b.institution AND path.title = b.title
    LEFT JOIN prior    ON prior.code = b.code AND prior.publish_date = b.publish_date
                      AND prior.institution = b.institution AND prior.title = b.title
    LEFT JOIN density  ON density.code = b.code AND density.publish_date = b.publish_date
                      AND density.institution = b.institution AND density.title = b.title
    LEFT JOIN firstcov fc ON fc.code = b.code AND fc.institution = b.institution
    LEFT JOIN afterreport ar ON ar.code = b.code AND ar.publish_date = b.publish_date
                      AND ar.institution = b.institution AND ar.title = b.title
    """


def report_outcomes(
    db: Database,
    period: tuple[dt.date, dt.date],
    *,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    main_horizon: int = MAIN_HORIZON,
) -> pd.DataFrame:
    """逐条研报的完整结果指标（终点收益 + 路径 + 语境）。

    返回列包含：
      ret_N          各期绝对收益
      excess_N       各期超额收益（减同月全样本中位数）
      max_gain/loss  持有期内最大浮盈 / 浮亏
      max_drawdown   持有期内最大回撤 ← 判断「拿不拿得住」
      days_to_N_pct  首次达到 +N% 所需交易日
      prior_ret_60   发布前 60 日走势 ← 追涨型 vs 逆势型
      coverage_30d   同期覆盖密度
      is_first_coverage / days_since_report
    """
    df = db.query(_outcome_sql(horizons, main_horizon), [period[0], period[1]])
    if df.empty:
        return df

    df["publish_date"] = pd.to_datetime(df["publish_date"])
    df["month"] = df["publish_date"].dt.to_period("M")

    # 超额收益：扣除同月全样本中位数。
    # 缺这一步会把大盘行情误判为研报有效。
    for h in horizons:
        col = f"ret_{h}"
        if col in df.columns:
            df[f"excess_{h}"] = df[col] - df.groupby("month")[col].transform("median")

    if "max_gain" in df.columns:
        df["excess_max_gain"] = (
            df["max_gain"] - df.groupby("month")["max_gain"].transform("median")
        )
    return df


# ----------------------------------------------------------------------
# 汇总视角
# ----------------------------------------------------------------------

def _agg(grp: pd.DataFrame, horizons: tuple[int, ...]) -> dict:
    """统一的分组汇总口径。样本数必须同时报告——小样本统计量不可信。"""
    row: dict = {"样本数": len(grp)}
    for h in horizons:
        col = f"excess_{h}"
        if col in grp.columns and grp[col].notna().any():
            row[f"超额{h}日"] = round(float(grp[col].median()), 4)
            row[f"胜率{h}日"] = round(float((grp[col] > 0).mean()), 3)
    for col, label in [("max_drawdown", "期内最大回撤"),
                       ("max_gain", "期内最大浮盈"),
                       ("max_loss", "期内最大浮亏")]:
        if col in grp.columns and grp[col].notna().any():
            row[label] = round(float(grp[col].median()), 4)
    for t in GAIN_TARGETS:
        col = f"days_to_{int(t * 100)}pct"
        if col in grp.columns:
            reached = grp[col].notna()
            row[f"达+{int(t*100)}%比例"] = round(float(reached.mean()), 3)
            if reached.any():
                row[f"达+{int(t*100)}%中位日"] = int(grp.loc[reached, col].median())
    return row


def by_rating(df: pd.DataFrame, horizons=DEFAULT_HORIZONS) -> pd.DataFrame:
    """按评级汇总。先验：无区分度（买入占 94%）。"""
    if df.empty:
        return df
    rows = [{"评级": r, **_agg(g, horizons)} for r, g in df.groupby("rating")]
    return pd.DataFrame(rows).sort_values("样本数", ascending=False)


def by_prior_trend(df: pd.DataFrame, horizons=DEFAULT_HORIZONS) -> pd.DataFrame:
    """按发布前走势分组：追涨型研报是否为反向指标。"""
    if df.empty or "prior_ret_60" not in df.columns:
        return pd.DataFrame()
    d = df.dropna(subset=["prior_ret_60"]).copy()
    if len(d) < 20:
        return pd.DataFrame()
    d["前期走势"] = pd.qcut(
        d["prior_ret_60"], 4,
        labels=["前期最弱", "前期偏弱", "前期偏强", "前期最强"], duplicates="drop")
    rows = [{"分组": str(k), **_agg(g, horizons)}
            for k, g in d.groupby("前期走势", observed=True)]
    return pd.DataFrame(rows)


def by_coverage_density(df: pd.DataFrame, horizons=DEFAULT_HORIZONS) -> pd.DataFrame:
    """按同期覆盖密度分组：扎堆推荐 vs 独家覆盖。"""
    if df.empty or "coverage_30d" not in df.columns:
        return pd.DataFrame()
    d = df.dropna(subset=["coverage_30d"]).copy()
    d["密度"] = pd.cut(d["coverage_30d"], [0, 1, 3, 8, 1000],
                       labels=["独家", "2-3家", "4-8家", "9家以上"])
    rows = [{"覆盖密度": str(k), **_agg(g, horizons)}
            for k, g in d.groupby("密度", observed=True)]
    return pd.DataFrame(rows)


def by_first_coverage(df: pd.DataFrame, horizons=DEFAULT_HORIZONS) -> pd.DataFrame:
    """首次覆盖 vs 持续跟踪。"""
    if df.empty or "is_first_coverage" not in df.columns:
        return pd.DataFrame()
    rows = [{"类型": "首次覆盖" if k else "持续跟踪", **_agg(g, horizons)}
            for k, g in df.groupby("is_first_coverage")]
    return pd.DataFrame(rows)


def rating_change_events(db: Database, period: tuple[dt.date, dt.date]) -> pd.DataFrame:
    """评级调整事件：同机构对同股票的前后评级变化。

    **本模块最值得关注的一项。** 评级绝对水平几乎无信息量（买入占 94%），
    但「变化」逆着激励机制——尤其是下调：分析师不愿得罪覆盖对象，
    敢下调通常意味着问题已较明确。
    """
    df = db.query(
        """
        WITH r AS (
            SELECT code, institution, publish_date, rating, title,
                   CASE rating
                       WHEN '买入' THEN 5 WHEN '增持' THEN 4
                       WHEN '持有' THEN 3 WHEN '中性' THEN 3
                       WHEN '减持' THEN 2 WHEN '卖出' THEN 1
                       ELSE NULL END AS score
            FROM research_report
            WHERE rating IS NOT NULL AND rating <> ''
              AND publish_date BETWEEN ? AND ?
        ),
        seq AS (
            SELECT *, lag(score) OVER w AS prev_score,
                      lag(rating) OVER w AS prev_rating
            FROM r WHERE score IS NOT NULL
            WINDOW w AS (PARTITION BY code, institution ORDER BY publish_date)
        )
        SELECT code, institution, publish_date, title, rating, prev_rating,
               score - prev_score AS delta
        FROM seq WHERE prev_score IS NOT NULL AND score <> prev_score
        """,
        [period[0], period[1]],
    )
    if df.empty:
        return df
    df["direction"] = df["delta"].apply(lambda d: "上调" if d > 0 else "下调")
    df["publish_date"] = pd.to_datetime(df["publish_date"])
    return df


def by_rating_change(
    db: Database, outcomes: pd.DataFrame, period: tuple[dt.date, dt.date],
    horizons=DEFAULT_HORIZONS,
) -> pd.DataFrame:
    """评级调整方向 × 后续表现。"""
    events = rating_change_events(db, period)
    if events.empty or outcomes.empty:
        return pd.DataFrame()

    merged = outcomes.merge(
        events[["code", "publish_date", "institution", "title", "direction",
                "prev_rating"]],
        on=["code", "publish_date", "institution", "title"], how="inner",
    )
    if merged.empty:
        return pd.DataFrame()
    rows = [{"方向": k, **_agg(g, horizons)} for k, g in merged.groupby("direction")]
    return pd.DataFrame(rows)


def run_full_study(
    db: Database, *, horizons: tuple[int, ...] = DEFAULT_HORIZONS
) -> dict[str, pd.DataFrame]:
    """完整研究：发现期与验证期分别输出，便于逐项对照。

    判定标准：**同一效应须在两期同向**，仅在发现期成立者视为噪音。
    """
    out: dict[str, pd.DataFrame] = {}
    inst_tables: dict[str, pd.DataFrame] = {}

    for label, period in [("发现期", IN_SAMPLE), ("验证期", OUT_SAMPLE)]:
        oc = report_outcomes(db, period, horizons=horizons)
        if oc.empty:
            out[f"{label}_无数据"] = pd.DataFrame()
            continue
        out[f"{label}_评级"] = by_rating(oc, horizons)
        out[f"{label}_评级调整"] = by_rating_change(db, oc, period, horizons)
        out[f"{label}_发布前走势"] = by_prior_trend(oc, horizons)
        out[f"{label}_覆盖密度"] = by_coverage_density(oc, horizons)
        out[f"{label}_首次覆盖"] = by_first_coverage(oc, horizons)

        inst = by_institution(oc, horizons=horizons)
        inst_tables[label] = inst
        if not inst.empty:
            out[f"{label}_机构"] = inst

        # 机构 × 行业：检验「领域专长」。样本极易稀疏，
        # 故同时输出样本量矩阵，供解读时判断可信度。
        inst_ind = by_institution_industry(oc, horizons=horizons)
        if not inst_ind.empty:
            out[f"{label}_机构×行业"] = inst_ind
        if label == "发现期":
            cov = coverage_matrix(oc)
            if not cov.empty:
                out["全期_样本量矩阵"] = cov

    # 机构排名的跨期稳定性 —— 比排名本身重要得多。
    # 若排名不可复现，任何「XX 证券最准」都只是对历史噪音的精确描述。
    if len(inst_tables) == 2:
        stability = institution_rank_stability(
            inst_tables.get("发现期", pd.DataFrame()),
            inst_tables.get("验证期", pd.DataFrame()))
        out["全期_机构排名稳定性"] = pd.DataFrame([
            {"检验项": k, "结果": v} for k, v in stability.items()])

    return out


# ----------------------------------------------------------------------
# 机构维度 —— 最容易过拟合，故附带稳定性检验
# ----------------------------------------------------------------------

def by_institution(
    df: pd.DataFrame,
    *,
    min_samples: int = 30,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> pd.DataFrame:
    """按机构汇总表现。

    ⚠️ **本视角最容易过拟合**：数百家机构 × 8 年，总有几家看起来神准，
    那大概率是随机波动。因此：
    - 强制最小样本数（默认 30 条），样本不足者直接剔除
    - 必须配合 institution_rank_stability 检验排名的跨期稳定性
    - 单看本表得出的「XX 证券最准」结论**不可采信**
    """
    if df.empty or "institution" not in df.columns:
        return pd.DataFrame()

    rows = []
    for inst, grp in df.groupby("institution"):
        if len(grp) < min_samples:
            continue
        rows.append({"机构": inst, **_agg(grp, horizons)})

    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    sort_col = f"超额{horizons[1]}日" if f"超额{horizons[1]}日" in out.columns else "样本数"
    return out.sort_values(sort_col, ascending=False).reset_index(drop=True)


def institution_rank_stability(
    in_sample: pd.DataFrame,
    out_sample: pd.DataFrame,
    *,
    metric: str = "超额60日",
) -> dict:
    """机构排名的跨期稳定性 —— 判断「哪家准」是真规律还是噪音。

    做法：取两期都有足够样本的机构，计算发现期排名与验证期排名的
    Spearman 秩相关系数。

        相关系数 ≈ 0   → 排名不可复现，机构维度纯属噪音，应弃用
        相关系数 显著为正 → 存在一定持续性，可谨慎作为参考权重

    **这个检验比排名本身重要得多。** 没有它，任何排行榜都只是
    对历史噪音的精确描述。
    """
    if in_sample.empty or out_sample.empty:
        return {"结论": "样本不足，无法检验"}
    if metric not in in_sample.columns or metric not in out_sample.columns:
        return {"结论": f"缺少指标 {metric}"}

    merged = in_sample[["机构", metric, "样本数"]].merge(
        out_sample[["机构", metric, "样本数"]],
        on="机构", suffixes=("_发现期", "_验证期"))

    if len(merged) < 5:
        return {"共同机构数": len(merged), "结论": "共同机构过少，无法检验"}

    # Spearman 秩相关 = 排名的 Pearson 相关。
    # 手工实现而非 method="spearman"：后者依赖 scipy，
    # 为一个函数引入数十 MB 依赖不划算。
    rho = (merged[f"{metric}_发现期"].rank()
           .corr(merged[f"{metric}_验证期"].rank()))

    # 发现期前 1/3 的机构，在验证期是否仍处于前 1/3
    n_top = max(1, len(merged) // 3)
    top_in = set(merged.nlargest(n_top, f"{metric}_发现期")["机构"])
    top_out = set(merged.nlargest(n_top, f"{metric}_验证期")["机构"])
    overlap = len(top_in & top_out) / n_top

    if pd.isna(rho):
        verdict = "无法计算"
    elif abs(rho) < 0.15:
        verdict = "❌ 排名不可复现，机构维度是噪音，不应作为参考权重"
    elif rho > 0.15:
        verdict = "⚠️ 存在弱持续性，可谨慎参考，但不足以单独作为决策依据"
    else:
        verdict = "❌ 呈负相关，更应警惕（可能是均值回归）"

    return {
        "共同机构数": len(merged),
        "秩相关系数": None if pd.isna(rho) else round(float(rho), 3),
        "发现期前1/3在验证期仍居前1/3的比例": round(overlap, 3),
        "随机基准": round(1 / 3, 3),
        "结论": verdict,
    }


def by_institution_industry(
    df: pd.DataFrame,
    *,
    min_samples: int = 30,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> pd.DataFrame:
    """机构 × 行业 —— 检验「领域专长」是否真实存在。

    直觉上成立：一家机构在新能源覆盖深入，不代表它在食品饮料同样可靠。
    但统计上极其脆弱：

        604 家机构 × 7 个行业 = 4200+ 组合，而样本仅约 1.8 万条研报
        平均每组合 4 条 —— 绝大多数组合的统计量毫无意义

    因此强制 min_samples 门槛，只保留样本充足的组合。
    实际能通过门槛的，大概率只有头部机构在其重点覆盖行业上。

    **本表同样必须配合跨期稳定性检验**——维度越细，过拟合越容易。
    """
    if df.empty or "institution" not in df.columns or "industry" not in df.columns:
        return pd.DataFrame()

    rows = []
    for (inst, ind), grp in df.groupby(["institution", "industry"]):
        if len(grp) < min_samples:
            continue
        rows.append({"机构×行业": f"{inst} / {ind}", **_agg(grp, horizons)})

    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    sort_col = f"超额{horizons[1]}日" if f"超额{horizons[1]}日" in out.columns else "样本数"
    return out.sort_values(sort_col, ascending=False).reset_index(drop=True)


def coverage_matrix(df: pd.DataFrame, *, top_n: int = 15) -> pd.DataFrame:
    """机构 × 行业的样本量矩阵 —— 先看清哪些组合根本没有足够数据。

    在解读任何分组结果之前应先看本表：样本量决定了结论的可信度，
    而稀疏的格子无论数字多漂亮都不可采信。
    """
    if df.empty or "industry" not in df.columns:
        return pd.DataFrame()
    top_inst = df["institution"].value_counts().head(top_n).index
    sub = df[df["institution"].isin(top_inst)]
    return (sub.pivot_table(index="institution", columns="industry",
                            values="code", aggfunc="count", fill_value=0)
              .reset_index())
