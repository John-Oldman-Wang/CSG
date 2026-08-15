"""数据采集管线。

**幂等自愈是本模块的核心约束**（见 docs/ARCHITECTURE.md 4.3）。

目标环境是一台会合盖休眠、会关机、会断网的 MacBook，且数据源（东财）
存在限流。因此采集必然中断——开发期实测连续请求二十余次即被限流。

所以不写「跑今天的数据」式任务，一律按：

    查本地水位 → 与目标区间比对 → 只补缺口 → 每单元成功即落水位

单只股票失败不影响其余，重跑时自动跳过已完成部分。
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable, Sequence

import pandas as pd

from csg.sources import akshare_source as aks
from csg.storage import Database

log = logging.getLogger(__name__)

DEFAULT_START = dt.date(2016, 1, 1)


class Ingestor:
    """采集协调器。每个 sync_* 方法都可安全重复执行。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # 基础信息
    # ------------------------------------------------------------------

    def sync_stock_basic(self) -> int:
        """在市 + 已退市股票列表。

        退市股必须入库：回测用今天的股票列表会自动剔除退市公司，
        产生幸存者偏差，凭空美化收益。
        """
        active = aks.fetch_stock_list()
        log.info("在市股票 %d 只", len(active))

        delisted = aks.fetch_delisted()
        log.info("退市股票 %d 只", len(delisted))

        frames = [active]
        if not delisted.empty:
            # 退市列表优先：其带有准确的上市/退市日期
            frames.append(delisted)
            active = active[~active["code"].isin(delisted["code"])]
            frames = [active, delisted]

        combined = pd.concat(frames, ignore_index=True)
        if "list_date" not in combined.columns:
            combined["list_date"] = pd.NaT

        cols = ["code", "name", "market", "exchange", "list_date",
                "delist_date", "is_active"]
        for c in cols:
            if c not in combined.columns:
                combined[c] = None

        n = self.db.upsert("stock_basic", combined[cols], ["code"])
        self.db.set_watermark("stock_basic", last_success_date=dt.date.today())
        log.info("stock_basic 写入 %d 行", n)
        return n

    def sync_industry(self, period: str = "20251231") -> int:
        df = aks.fetch_industry(period)
        cols = ["code", "taxonomy", "industry_name", "start_date", "end_date"]
        n = self.db.upsert("industry_member", df[cols],
                           ["code", "taxonomy", "industry_name"])
        self.db.set_watermark("industry_member", last_success_date=dt.date.today())
        log.info("industry_member 写入 %d 行", n)
        return n

    # ------------------------------------------------------------------
    # 行情
    # ------------------------------------------------------------------

    def sync_daily_quotes(
        self,
        codes: Sequence[str],
        *,
        start: dt.date = DEFAULT_START,
        end: dt.date | None = None,
        force: bool = False,
    ) -> dict[str, int]:
        """逐只采集日线。

        水位粒度为单只股票，因此中断后重跑只补未完成的部分。
        """
        end = end or dt.date.today()
        stats = {"ok": 0, "skipped": 0, "failed": 0, "rows": 0}

        for i, code in enumerate(codes, 1):
            wm = None if force else self.db.get_watermark("daily_quote", code)
            fetch_start = start
            if wm is not None:
                if wm >= end:
                    stats["skipped"] += 1
                    continue
                # 回退一日，容忍边界处的部分写入
                fetch_start = max(start, wm - dt.timedelta(days=1))

            try:
                df = aks.fetch_daily_quote(code, fetch_start, end)
                if df.empty:
                    # 空返回可能是尚未上市或长期停牌，属正常
                    self.db.set_watermark("daily_quote", code,
                                          last_success_date=end, status="ok")
                    stats["skipped"] += 1
                    continue

                cols = ["code", "trade_date", "open", "high", "low", "close",
                        "volume", "amount", "pct_chg", "turnover", "adj_factor"]
                rows = self.db.upsert("daily_quote", df[cols], ["code", "trade_date"])
                self.db.set_watermark("daily_quote", code,
                                      last_success_date=df["trade_date"].max(),
                                      status="ok")
                stats["ok"] += 1
                stats["rows"] += rows
            except Exception as exc:  # noqa: BLE001 — 单只失败必须隔离
                self.db.set_watermark("daily_quote", code, last_success_date=wm,
                                      status="failed", error=str(exc)[:300])
                stats["failed"] += 1
                log.warning("[%d/%d] %s 行情失败: %s", i, len(codes), code, exc)

            if i % 25 == 0:
                log.info("行情进度 %d/%d %s", i, len(codes), stats)

        return stats

    # ------------------------------------------------------------------
    # 财务
    # ------------------------------------------------------------------

    def sync_financials(
        self,
        codes: Sequence[str],
        *,
        statements: Iterable[aks.Statement] = ("income", "balance", "cashflow"),
        force: bool = False,
    ) -> dict[str, int]:
        """逐只采集三大报表。

        必须使用东财「按报告期」详细接口——其 NOTICE_DATE 为真实首次披露日，
        已与巨潮原始公告交叉验证一致。批量接口的公告日期与报告期错位，
        用于 PIT 会静默产生未来函数。
        """
        stats = {"ok": 0, "skipped": 0, "failed": 0, "rows": 0}
        statements = list(statements)
        today = dt.date.today()

        for i, code in enumerate(codes, 1):
            for stmt in statements:
                table = aks.target_table(stmt)
                dataset = f"{table}"

                if not force:
                    wm = self.db.get_watermark(dataset, code)
                    # 财报季度更新，30 天内已同步过则跳过
                    if wm is not None and (today - wm).days < 30:
                        stats["skipped"] += 1
                        continue

                try:
                    df = aks.fetch_financial(code, stmt)
                    if df.empty:
                        self.db.set_watermark(dataset, code,
                                              last_success_date=today, status="ok")
                        stats["skipped"] += 1
                        continue

                    rows = self.db.upsert(table, df, ["code", "report_period"])
                    self.db.set_watermark(dataset, code,
                                          last_success_date=today, status="ok")
                    stats["ok"] += 1
                    stats["rows"] += rows
                except Exception as exc:  # noqa: BLE001
                    self.db.set_watermark(dataset, code, status="failed",
                                          error=str(exc)[:300])
                    stats["failed"] += 1
                    log.warning("[%d/%d] %s %s 失败: %s", i, len(codes), code, stmt, exc)

            if i % 10 == 0:
                log.info("财务进度 %d/%d %s", i, len(codes), stats)

        return stats

    # ------------------------------------------------------------------
    # 研报
    # ------------------------------------------------------------------

    def sync_research(
        self,
        codes: Sequence[str],
        *,
        refresh_days: int = 7,
        force: bool = False,
    ) -> dict[str, int]:
        """研报与盈利预测。

        `refresh_days` 控制重采频率：研报持续新增，但无需每日重拉。
        每次采集写入新的 snapshot_date，历史快照全部保留——
        这是积累「一致预期随时间变动」序列的唯一途径，而该序列
        无法向历史回溯获取（实测 2018-2023 年研报的预测字段全为空）。
        """
        stats = {"ok": 0, "skipped": 0, "failed": 0, "reports": 0, "forecasts": 0}
        today = dt.date.today()

        for i, code in enumerate(codes, 1):
            if not force:
                wm = self.db.get_watermark("research_report", code)
                if wm is not None and (today - wm).days < refresh_days:
                    stats["skipped"] += 1
                    continue

            try:
                rep, fc = aks.fetch_research_reports(code)
                if rep.empty:
                    self.db.set_watermark("research_report", code,
                                          last_success_date=today, status="ok")
                    stats["skipped"] += 1
                    continue

                stats["reports"] += self.db.upsert(
                    "research_report", rep,
                    ["code", "publish_date", "institution", "title"])
                if not fc.empty:
                    stats["forecasts"] += self.db.upsert(
                        "research_forecast", fc,
                        ["code", "publish_date", "institution",
                         "forecast_year", "snapshot_date"])

                self.db.set_watermark("research_report", code,
                                      last_success_date=today, status="ok")
                stats["ok"] += 1
            except Exception as exc:  # noqa: BLE001
                self.db.set_watermark("research_report", code, status="failed",
                                      error=str(exc)[:300])
                stats["failed"] += 1
                log.warning("[%d/%d] %s 研报失败: %s", i, len(codes), code, exc)

            if i % 25 == 0:
                log.info("研报进度 %d/%d %s", i, len(codes), stats)

        return stats

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------

    def health_report(self, stale_days: int = 7) -> pd.DataFrame:
        """水位陈旧与失败条目汇总。

        调度静默失效是本环境最危险的故障：系统「看起来在跑」，
        实际早已停止，而没有任何报错。
        """
        return self.db.query(
            """
            SELECT dataset,
                   count(*)                                      AS scopes,
                   sum(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                   sum(CASE WHEN last_success_date IS NULL
                             OR last_success_date < current_date - ?
                            THEN 1 ELSE 0 END)                   AS stale,
                   min(last_success_date)                        AS oldest,
                   max(last_success_date)                        AS newest
            FROM sync_watermark
            GROUP BY dataset
            ORDER BY dataset
            """,
            [stale_days],
        )

    def retry_failed(self, dataset: str) -> list[str]:
        """返回该数据集下失败的 scope，供定向重试。"""
        df = self.db.query(
            "SELECT scope FROM sync_watermark WHERE dataset = ? AND status = 'failed'",
            [dataset],
        )
        return df["scope"].tolist()
