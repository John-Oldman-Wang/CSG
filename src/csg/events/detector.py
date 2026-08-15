"""事件检测器。

扫描已入库的数据，产出事件。**不做实时推送**——目标环境是一台会休眠、
会关机的 MacBook，真正的实时做不到（ARCHITECTURE.md 4.4）。
实际形态是「定时批扫描 + 完整的事件语义与生命周期」。

这与方法论自洽：持仓假设是否被证伪，晚几小时知道不改变结论。

**幂等是硬要求。** event_id 由事件的自然键哈希得到，重复扫描不产生
重复事件。休眠唤醒后的补跑会重扫大量历史数据，没有这条会瞬间刷屏。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging

import pandas as pd

from csg.storage import Database

log = logging.getLogger(__name__)

# 评级到分值，用于识别调整方向
RATING_SCORE = {"买入": 5, "增持": 4, "持有": 3, "中性": 3, "减持": 2, "卖出": 1}


def make_event_id(event_type: str, code: str, ref_date: dt.date, ref_key: str = "") -> str:
    """事件的自然键哈希 —— 幂等去重的基础。"""
    raw = f"{event_type}|{code}|{ref_date}|{ref_key}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class Detector:
    """事件检测器集合。

    每个 detect_* 方法扫描一类数据源，返回待写入的事件 DataFrame。
    调用方负责落库（upsert 天然幂等）。
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # 研报事件
    # ------------------------------------------------------------------

    def detect_rating_changes(
        self,
        *,
        since: dt.date | None = None,
        only_tracked: bool = True,
    ) -> pd.DataFrame:
        """评级调整事件。

        **只关注调整，不关注绝对评级。** 实测买入评级占 94%，
        绝对水平几乎不携带信息；而「变化」——尤其是下调——逆着
        分析师的激励机制（不愿得罪覆盖对象），更可能有信息量。

        `only_tracked=True` 时仅对观察池与持仓产生事件：
        对不关心的股票发提醒，是提醒系统自我失效的最快途径。
        """
        since = since or (dt.date.today() - dt.timedelta(days=30))

        scope_filter = ""
        if only_tracked:
            scope_filter = "AND r.code IN (SELECT code FROM watchlist)"

        df = self.db.query(
            f"""
            WITH scored AS (
                SELECT r.code, r.institution, r.publish_date, r.rating, r.title,
                       CASE r.rating
                           WHEN '买入' THEN 5 WHEN '增持' THEN 4
                           WHEN '持有' THEN 3 WHEN '中性' THEN 3
                           WHEN '减持' THEN 2 WHEN '卖出' THEN 1
                       END AS score
                FROM research_report r
                WHERE r.rating IS NOT NULL AND r.rating <> ''
                  {scope_filter}
            ),
            seq AS (
                SELECT *,
                       lag(score)  OVER w AS prev_score,
                       lag(rating) OVER w AS prev_rating
                FROM scored WHERE score IS NOT NULL
                WINDOW w AS (PARTITION BY code, institution ORDER BY publish_date)
            )
            SELECT code, institution, publish_date, rating, prev_rating,
                   title, score - prev_score AS delta
            FROM seq
            WHERE prev_score IS NOT NULL
              AND score <> prev_score
              AND publish_date >= ?
            ORDER BY publish_date DESC
            """,
            [since],
        )
        if df.empty:
            return pd.DataFrame()

        rows = []
        for _, r in df.iterrows():
            downgrade = r["delta"] < 0
            etype = "research_downgrade" if downgrade else "research_upgrade"
            ref_date = pd.Timestamp(r["publish_date"]).date()
            rows.append({
                "event_id": make_event_id(etype, r["code"], ref_date, r["institution"]),
                "event_type": etype,
                "code": r["code"],
                "ref_date": ref_date,
                "ref_key": r["institution"],
                # 一律 P2（仅记录，不打扰）。
                #
                # 原设计给下调 P1，依据是「逆激励机制」的先验推理。
                # 该假设已被验证④ 证伪（run_id 见 validation_conclusion）：
                #     发现期 下调超额60日 +1.62%，验证期 −3.00%，符号完全反转
                # 两期反向是噪音的典型特征。继续按 P1 推送等于往飞书发噪音。
                #
                # 事件仍然记录：一是留作样本积累，二是它作为**上下文**
                # 附在其他任务里仍有参考价值——只是不再单独构成打扰你的理由。
                "severity": "P2",
                "title": f"{r['institution']} 将评级由 {r['prev_rating']} "
                         f"{'下调' if downgrade else '上调'}至 {r['rating']}",
                "payload": json.dumps({
                    "institution": r["institution"],
                    "prev_rating": r["prev_rating"],
                    "rating": r["rating"],
                    "delta": int(r["delta"]),
                    "report_title": r["title"],
                }, ensure_ascii=False),
                "is_backfill": False,
            })
        return pd.DataFrame(rows)

    def detect_coverage_surge(
        self,
        *,
        since: dt.date | None = None,
        window_days: int = 30,
        threshold: int = 8,
    ) -> pd.DataFrame:
        """覆盖密度激增。

        ⚠️ 这**可能是反向指标**：券商扎堆覆盖往往发生在股价已经涨完之后。
        在验证④ 给出结论之前，本事件仅记录、不推送（severity=P2），
        用于积累样本。
        """
        since = since or (dt.date.today() - dt.timedelta(days=window_days))
        df = self.db.query(
            """
            SELECT code, publish_date, count(*) AS n
            FROM research_report
            WHERE publish_date >= ?
              AND code IN (SELECT code FROM watchlist)
            GROUP BY code, publish_date
            HAVING count(*) >= ?
            """,
            [since, threshold],
        )
        if df.empty:
            return pd.DataFrame()

        rows = []
        for _, r in df.iterrows():
            ref_date = pd.Timestamp(r["publish_date"]).date()
            rows.append({
                "event_id": make_event_id("coverage_surge", r["code"], ref_date),
                "event_type": "coverage_surge",
                "code": r["code"],
                "ref_date": ref_date,
                "ref_key": "",
                "severity": "P2",
                "title": f"单日 {int(r['n'])} 家机构发布研报",
                "payload": json.dumps({"count": int(r["n"])}, ensure_ascii=False),
                "is_backfill": False,
            })
        return pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # 财报事件
    # ------------------------------------------------------------------

    def detect_report_disclosure(
        self, *, since: dt.date | None = None
    ) -> pd.DataFrame:
        """财报披露事件 —— 触发证伪条件核对。

        以 disclosure_date 判定，而非 report_period：
        投资者是在财报公布那天才知道内容的。
        """
        since = since or (dt.date.today() - dt.timedelta(days=30))
        df = self.db.query(
            """
            SELECT i.code, i.report_period, i.disclosure_date,
                   i.n_income_attr_p, i.total_revenue
            FROM fin_income i
            WHERE i.disclosure_date >= ?
              AND i.code IN (SELECT code FROM watchlist)
            """,
            [since],
        )
        if df.empty:
            return pd.DataFrame()

        rows = []
        for _, r in df.iterrows():
            ref_date = pd.Timestamp(r["disclosure_date"]).date()
            period = pd.Timestamp(r["report_period"]).date()
            rows.append({
                "event_id": make_event_id("report_disclosed", r["code"], period),
                "event_type": "report_disclosed",
                "code": r["code"],
                "ref_date": ref_date,
                "ref_key": str(period),
                "severity": "P1",
                "title": f"{period} 财报披露",
                "payload": json.dumps({
                    "report_period": str(period),
                    "revenue": float(r["total_revenue"]) if pd.notna(r["total_revenue"]) else None,
                    "net_profit": float(r["n_income_attr_p"]) if pd.notna(r["n_income_attr_p"]) else None,
                }, ensure_ascii=False),
                "is_backfill": False,
            })
        return pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # 价格事件
    # ------------------------------------------------------------------

    def detect_excess_drawdown(
        self,
        *,
        lookback_days: int = 20,
        excess_threshold: float = -0.15,
    ) -> pd.DataFrame:
        """超额下跌事件。

        **拆出超额跌幅，而非绝对跌幅。** 跟随行业一起跌与独自下跌是
        两种完全不同的事：前者多为贝塔/情绪，后者往往有个股特定原因。
        系统自动做这个拆分，不让人从绝对跌幅去猜（METHODOLOGY ⑥ 规则 2）。
        """
        df = self.db.query(
            """
            WITH latest AS (SELECT max(trade_date) AS d FROM daily_quote),
            span AS (
                SELECT q.code, q.trade_date, q.close * q.adj_factor AS p,
                       row_number() OVER (PARTITION BY q.code
                                          ORDER BY q.trade_date DESC) AS rn
                FROM daily_quote q, latest
                WHERE q.trade_date <= latest.d
            ),
            ret AS (
                SELECT a.code, max(latest.d) AS as_of,
                       (max(CASE WHEN a.rn = 1 THEN a.p END) /
                        nullif(max(CASE WHEN a.rn = ? THEN a.p END), 0)) - 1 AS r
                FROM span a, latest
                WHERE a.rn IN (1, ?)
                GROUP BY a.code
            ),
            ind AS (
                SELECT im.industry_name, median(ret.r) AS bench
                FROM ret JOIN industry_member im ON im.code = ret.code
                WHERE im.taxonomy = 'em_industry'
                GROUP BY im.industry_name
            )
            SELECT ret.code, ret.as_of, ret.r AS stock_ret,
                   ind.bench AS industry_ret,
                   ret.r - ind.bench AS excess
            FROM ret
            JOIN industry_member im ON im.code = ret.code AND im.taxonomy = 'em_industry'
            JOIN ind ON ind.industry_name = im.industry_name
            WHERE ret.code IN (SELECT code FROM watchlist)
              AND ret.r - ind.bench <= ?
            """,
            [lookback_days, lookback_days, excess_threshold],
        )
        if df.empty:
            return pd.DataFrame()

        rows = []
        for _, r in df.iterrows():
            ref_date = pd.Timestamp(r["as_of"]).date()
            rows.append({
                "event_id": make_event_id("excess_drawdown", r["code"], ref_date),
                "event_type": "excess_drawdown",
                "code": r["code"],
                "ref_date": ref_date,
                "ref_key": "",
                "severity": "P1",
                "title": f"近 {lookback_days} 日超额下跌 {r['excess']:.1%}"
                         f"（个股 {r['stock_ret']:.1%} vs 行业 {r['industry_ret']:.1%}）",
                "payload": json.dumps({
                    "stock_return": round(float(r["stock_ret"]), 4),
                    "industry_return": round(float(r["industry_ret"]), 4),
                    "excess": round(float(r["excess"]), 4),
                    "lookback_days": lookback_days,
                }, ensure_ascii=False),
                "is_backfill": False,
            })
        return pd.DataFrame(rows)

    # ------------------------------------------------------------------

    def run_all(self, *, since: dt.date | None = None) -> int:
        """执行全部检测器并落库。返回新增事件数。

        upsert 按 event_id 覆盖，故重复扫描不产生重复事件。
        """
        frames = []
        for name, fn in [
            ("rating_changes", lambda: self.detect_rating_changes(since=since)),
            ("coverage_surge", lambda: self.detect_coverage_surge(since=since)),
            ("report_disclosure", lambda: self.detect_report_disclosure(since=since)),
            ("excess_drawdown", self.detect_excess_drawdown),
        ]:
            try:
                df = fn()
                if not df.empty:
                    frames.append(df)
                    log.info("检测器 %s 产出 %d 个事件", name, len(df))
            except Exception as exc:  # noqa: BLE001 — 单个检测器失败不应中断其余
                log.warning("检测器 %s 失败: %s", name, exc)

        if not frames:
            return 0

        allev = pd.concat(frames, ignore_index=True).drop_duplicates("event_id")
        before = self.db.query("SELECT count(*) AS n FROM event")["n"].iloc[0]
        self.db.upsert("event", allev, ["event_id"])
        after = self.db.query("SELECT count(*) AS n FROM event")["n"].iloc[0]
        return int(after - before)
