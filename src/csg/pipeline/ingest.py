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
import time
from collections.abc import Iterable, Sequence

import pandas as pd

from csg.sources import akshare_source as aks
from csg.sources import baostock_source as bss
from csg.sources import ths_source as ths
from csg.sources.base import hard_timeout
from csg.storage import Database

log = logging.getLogger(__name__)

# 单只股票的单次采集硬上限（秒）。超过即判失败并继续下一只——
# 一只股票卡住不该让整条链停摆。见 sources/base.hard_timeout。
PER_CODE_TIMEOUT = 180.0

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
        deadline: float | None = None,
        source: str = "auto",
    ) -> dict[str, int]:
        """逐只采集日线。

        水位粒度为单只股票，因此中断后重跑只补未完成的部分。

        `source`：auto 优先 akshare（东财），失败自动切 baostock。
        东财在请求量偏大后会限流封禁，实测行情接口可连续全部失败；
        baostock 非爬虫架构，不受其影响。

        ⚠️ 两源复权算法不完全一致，**同一只股票不应混用**，
        否则会在拼接处产生虚假价格跳变。因此按股票整体选源，
        一旦某只切到 baostock，其全部历史都由 baostock 提供。
        """
        end = end or dt.date.today()
        stats = {"ok": 0, "skipped": 0, "failed": 0, "rows": 0, "未处理": 0}

        for i, code in enumerate(codes, 1):
            if deadline is not None and time.monotonic() > deadline:
                stats["未处理"] = len(codes) - i + 1
                log.info("行情达时间预算，剩余 %d 只留待下次", stats["未处理"])
                break
            wm = None if force else self.db.get_watermark("daily_quote", code)
            fetch_start = start
            if wm is not None:
                if wm >= end:
                    stats["skipped"] += 1
                    continue
                # 回退一日，容忍边界处的部分写入
                fetch_start = max(start, wm - dt.timedelta(days=1))

            try:
                # 单只硬上限。循环顶部的时间预算救不了「单只内部卡死」——
                # 2026-08-18 那次就卡在 000503 切到 baostock 之后，
                # 3 天 16 小时、86% CPU 空转，全程持锁。
                with hard_timeout(PER_CODE_TIMEOUT, f"{code} 行情"):
                    df = self._fetch_quote(code, fetch_start, end, source)
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

    def _fetch_quote(
        self, code: str, start: dt.date, end: dt.date, source: str
    ) -> pd.DataFrame:
        """按 source 策略取行情。auto 模式下东财失败即切 baostock。"""
        if source == "baostock":
            return bss.fetch_daily_quote(code, start, end)
        if source == "akshare":
            return aks.fetch_daily_quote(code, start, end)

        try:
            return aks.fetch_daily_quote(code, start, end)
        except Exception as exc:  # noqa: BLE001
            log.info("%s 东财失败(%s)，切换 baostock", code, type(exc).__name__)
            return bss.fetch_daily_quote(code, start, end)

    # ------------------------------------------------------------------
    # 财务
    # ------------------------------------------------------------------

    def sync_financials(
        self,
        codes: Sequence[str],
        *,
        statements: Iterable[aks.Statement] = ("income", "balance", "cashflow"),
        force: bool = False,
        deadline: float | None = None,
    ) -> dict[str, int]:
        """逐只采集三大报表。

        必须使用东财「按报告期」详细接口——其 NOTICE_DATE 为真实首次披露日，
        已与巨潮原始公告交叉验证一致。批量接口的公告日期与报告期错位，
        用于 PIT 会静默产生未来函数。
        """
        stats = {"ok": 0, "skipped": 0, "failed": 0, "rows": 0, "未处理": 0}
        statements = list(statements)
        today = dt.date.today()

        for i, code in enumerate(codes, 1):
            # 到点即停。水位保证续跑不重做，故「没跑完」不是失败，
            # 是把剩下的留给下一次——总比占着写锁跑 20 小时好。
            if deadline is not None and time.monotonic() > deadline:
                stats["未处理"] = len(codes) - i + 1
                log.info("财报达时间预算，剩余 %d 只留待下次", stats["未处理"])
                break
            t0 = time.monotonic()
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
                    with hard_timeout(PER_CODE_TIMEOUT, f"{code} {stmt}"):
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

            # 逐只记录耗时：接口响应时间波动极大（实测 6-60 秒/表），
            # 只报进度不报速度时无法区分「慢」与「卡住」。
            # 阈值 1 秒用于滤掉水位命中的跳过项——它们不发请求，
            # 记录只会淹没真正的采集条目。
            elapsed = time.monotonic() - t0
            if elapsed >= 1.0:
                log.info("[%d/%d] %s 用时 %.0fs %s", i, len(codes), code,
                         elapsed, stats)
            elif i % 50 == 0:
                log.info("[%d/%d] 跳过至此（水位命中）", i, len(codes))

        return stats

    def sync_valuations(
        self,
        codes: Sequence[str],
        *,
        start: dt.date = DEFAULT_START,
        end: dt.date | None = None,
        force: bool = False,
    ) -> dict[str, int]:
        """每日估值指标（PE/PB/PS）—— L0 轻量层。

        数据源为 baostock：akshare 没有稳定的历史 PE 批量接口，
        而 baostock 的 K 线接口原生附带 peTTM/pbMRQ/psTTM，
        正好填补这一空缺。

        ⚠️ baostock 不提供市值字段。市值需由「收盘价 × 总股本」计算，
        而总股本数据尚未采集，故 total_mv/circ_mv 暂为 NULL。
        前端须显示「—」而非 0 —— 缺失与零是两回事。
        """
        end = end or dt.date.today()
        stats = {"ok": 0, "skipped": 0, "failed": 0, "rows": 0}

        for i, code in enumerate(codes, 1):
            wm = None if force else self.db.get_watermark("daily_basic", code)
            if wm is not None and wm >= end:
                stats["skipped"] += 1
                continue

            try:
                df = bss.fetch_valuation(code, start, end)
                if df.empty:
                    self.db.set_watermark("daily_basic", code,
                                          last_success_date=end, status="ok")
                    stats["skipped"] += 1
                    continue

                cols = ["code", "trade_date", "pe_ttm", "pb", "ps_ttm",
                        "dv_ratio", "total_mv", "circ_mv"]
                rows = self.db.upsert("daily_basic", df[cols], ["code", "trade_date"])
                self.db.set_watermark("daily_basic", code,
                                      last_success_date=df["trade_date"].max(),
                                      status="ok")
                stats["ok"] += 1
                stats["rows"] += rows
            except Exception as exc:  # noqa: BLE001
                self.db.set_watermark("daily_basic", code, last_success_date=wm,
                                      status="failed", error=str(exc)[:300])
                stats["failed"] += 1
                log.warning("[%d/%d] %s 估值失败: %s", i, len(codes), code, exc)

            if i % 25 == 0:
                log.info("估值进度 %d/%d %s", i, len(codes), stats)

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
        source: str = "em",
        deadline: float | None = None,
    ) -> dict[str, int]:
        """研报与盈利预测。

        `refresh_days` 控制重采频率：研报持续新增，但无需每日重拉。
        每次采集写入新的 snapshot_date，历史快照全部保留——
        这是积累「一致预期随时间变动」序列的唯一途径，而该序列
        无法向历史回溯获取（实测 2018-2023 年研报的预测字段全为空）。
        """
        # 水位按数据源分开记：两个源的覆盖面与更新节奏不同，
        # 共用一个水位会让先跑的那个把后跑的整体跳过。
        ds = "research_report" if source == "em" else f"research_report_{source}"
        fetch = (aks.fetch_research_reports if source == "em"
                 else ths.fetch_research)

        stats = {"ok": 0, "skipped": 0, "failed": 0, "reports": 0,
                 "forecasts": 0, "未处理": 0}
        today = dt.date.today()

        for i, code in enumerate(codes, 1):
            if deadline is not None and time.monotonic() > deadline:
                stats["未处理"] = len(codes) - i + 1
                log.info("研报[%s]达时间预算，剩余 %d 只留待下次",
                         source, stats["未处理"])
                break
            # ⚠️ 进度打印必须在**循环体开头**，不能放末尾。
            #
            # 事故（2026-08-17）：原先放在末尾，而它前面有两处 continue
            # （水位跳过、该股无研报）。于是第 250/275/300 只恰好被跳过时，
            # 那三行进度**永远不会打印**——日志从 225 直接跳到 325，
            # 中间静默 10 分钟。
            #
            # 最坏之处在于：跳过越多、日志越安静，而「跳过很多」恰恰
            # 发生在续跑时——那正是你最需要确认它还活着的时候。
            # 我据此误判为进程挂起，几乎错杀了一个正常运行的采集。
            if i % 25 == 1 or i == len(codes):
                log.info("研报进度 %d/%d %s", i - 1, len(codes), stats)

            if not force:
                wm = self.db.get_watermark(ds, code)
                if wm is not None and (today - wm).days < refresh_days:
                    stats["skipped"] += 1
                    continue

            try:
                with hard_timeout(PER_CODE_TIMEOUT, f"{code} 研报[{source}]"):
                    rep, fc = fetch(code)
                if rep.empty:
                    self.db.set_watermark(ds, code,
                                          last_success_date=today, status="ok")
                    stats["skipped"] += 1
                    continue

                stats["reports"] += self.db.upsert(
                    "research_report", rep,
                    ["code", "publish_date", "institution", "title", "source"])
                if not fc.empty:
                    stats["forecasts"] += self.db.upsert(
                        "research_forecast", fc,
                        ["code", "publish_date", "institution",
                         "forecast_year", "snapshot_date", "source"])

                self.db.set_watermark(ds, code,
                                      last_success_date=today, status="ok")
                stats["ok"] += 1
            except Exception as exc:  # noqa: BLE001
                self.db.set_watermark(ds, code, status="failed",
                                      error=str(exc)[:300])
                stats["failed"] += 1
                log.warning("[%d/%d] %s 研报失败: %s", i, len(codes), code, exc)

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
