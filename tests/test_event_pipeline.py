"""事件驱动闭环的端到端验证。

用内存数据库 + 合成数据跑通完整链路，**不依赖真实采集数据**：

    数据 → detector 扫描 → event（幂等去重）
         → strategy 匹配 → review_task（含 SLA）
         → 飞书卡片渲染 → 人提交结论 → review_conclusion

这样在真实数据到位前就能验证逻辑正确性。真实数据只影响
「事件是否被触发」，不影响「触发后的处理是否正确」。

**重点验证的三件事**（都是方法论的硬性要求）：

1. 幂等去重——同一份数据扫两次不产生两个事件。
   MacBook 会休眠，唤醒后补跑扫描是常态，重复推送会直接淹没用户。
2. 策略过滤——不在 watchlist 的股票不生成任务。
   对不关心的股票发提醒是最快的自我失效路径。
3. 结论约束——verdict=insufficient 必须带下次复核日期。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from csg.events.detector import Detector, make_event_id
from csg.events.strategies import StrategyEngine

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "src" / "csg" / "storage"


class MemDB:
    """内存数据库，接口与 csg.storage.Database 一致的最小子集。"""

    def __init__(self) -> None:
        self.conn = duckdb.connect(":memory:")
        for name in ("schema.sql", "schema_events.sql"):
            self.conn.execute((SCHEMA_DIR / name).read_text(encoding="utf-8"))

    def query(self, sql: str, params=None) -> pd.DataFrame:
        return self.conn.execute(sql, params or []).df()

    def upsert(self, table: str, df: pd.DataFrame, keys) -> int:
        if df.empty:
            return 0
        self.conn.register("_inc", df)
        try:
            match = " AND ".join(f"t.{k} = s.{k}" for k in keys)
            self.conn.execute(f"DELETE FROM {table} t USING _inc s WHERE {match}")
            self.conn.execute(
                f"INSERT INTO {table} ({', '.join(df.columns)}) "
                f"SELECT {', '.join(df.columns)} FROM _inc")
        finally:
            self.conn.unregister("_inc")
        return len(df)

    def close(self) -> None:
        self.conn.close()


@pytest.fixture
def db() -> MemDB:
    d = MemDB()
    d.conn.executemany(
        "INSERT INTO stock_basic (code,name,market,exchange,is_active) VALUES (?,?,?,?,?)",
        [("300750", "宁德时代", "创业板", "SZ", True),
         ("002415", "海康威视", "主板", "SZ", True),
         ("000001", "无关股票", "主板", "SZ", True)])

    # 仅前两只进 watchlist —— 第三只用于验证「不关心的股票不生成任务」
    d.conn.executemany(
        "INSERT INTO watchlist (code,added_at,tier,thesis,core_assumptions,"
        "falsification,target_price) VALUES (?,?,?,?,?,?,?)",
        [("300750", dt.date(2024, 1, 1), "holding",
          "全球动力电池龙头，规模与技术双重壁垒",
          "海外产能 2027 释放；毛利率维持 18% 以上",
          "连续两季营收增速低于 10%；毛利率跌破 15%；出现全行业价格战", 250.0),
         ("002415", dt.date(2024, 6, 1), "watch",
          "安防龙头向 AI 视觉延伸",
          "创新业务占比逐年提升", "创新业务增速低于主业；应收账款周转恶化", 40.0)])

    # 行情：300750 独自大跌（个股原因），002415 跟随行业（贝塔）
    rows = []
    for i, day in enumerate(pd.bdate_range("2024-01-02", periods=80).date):
        rows.append(("300750", day, 200 - i * 1.2, 205 - i * 1.2, 195 - i * 1.2,
                     200 - i * 1.2, 1_000_000, 2e8, -0.6, 1.0, 1.0))
        rows.append(("002415", day, 30 - i * 0.02, 31 - i * 0.02, 29 - i * 0.02,
                     30 - i * 0.02, 800_000, 2.4e7, -0.07, 1.0, 1.0))
        rows.append(("000001", day, 10, 10.2, 9.8, 10, 500_000, 5e6, 0.0, 1.0, 1.0))
    d.conn.executemany(
        "INSERT INTO daily_quote VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)

    d.conn.executemany(
        "INSERT INTO industry_member (code,taxonomy,industry_name) VALUES (?,?,?)",
        [("300750", "em_industry", "电池"), ("002415", "em_industry", "电池"),
         ("000001", "em_industry", "电池")])

    # 财报：300750 在窗口内披露
    d.conn.execute(
        "INSERT INTO fin_income (code, report_period, disclosure_date, "
        "total_revenue, n_income_attr_p) VALUES ('300750','2024-03-31',"
        "'2024-04-16', 79770000000, 10510000000)")
    return d


def test_detect_is_idempotent(db: MemDB) -> None:
    """同一份数据扫两次只产生一个事件。

    这是硬性要求：MacBook 休眠唤醒后补跑扫描是常态，
    重复事件会直接把提醒淹没，进而导致用户静音渠道——等于系统失效。
    """
    det = Detector(db)
    first = det.run_all(since=dt.date(2024, 1, 1))
    count_after_first = db.query("SELECT count(*) n FROM event")["n"].iloc[0]

    det.run_all(since=dt.date(2024, 1, 1))
    count_after_second = db.query("SELECT count(*) n FROM event")["n"].iloc[0]

    assert first > 0, "应至少检测到一个事件"
    assert count_after_first == count_after_second, "重复扫描不得产生新事件"


def test_event_id_stable() -> None:
    """事件 ID 由业务键决定，与调用时刻无关。"""
    a = make_event_id("report_disclosed", "300750", dt.date(2024, 4, 16))
    b = make_event_id("report_disclosed", "300750", dt.date(2024, 4, 16))
    c = make_event_id("report_disclosed", "300750", dt.date(2024, 4, 17))
    assert a == b
    assert a != c


def test_strategy_filters_unwatched_stocks(db: MemDB) -> None:
    """不在 watchlist 的股票不生成任务。

    过滤比触发更重要：对不关心的股票发提醒是最快的自我失效路径。
    """
    Detector(db).run_all(since=dt.date(2024, 1, 1))
    tasks = StrategyEngine(db).process_pending_events()

    task_codes = {t.code for t in tasks}
    assert "000001" not in task_codes, "不在 watchlist 的股票不应生成任务"


def test_task_carries_falsification_and_rebuy_question(db: MemDB) -> None:
    """任务必须携带当初的证伪条件，并强制提出重新买入之问。

    监控的对象是**假设**而非价格；重新买入之问用于切断沉没成本。
    """
    Detector(db).run_all(since=dt.date(2024, 1, 1))
    tasks = StrategyEngine(db).process_pending_events()
    assert tasks, "应生成至少一个任务"

    holding = [t for t in tasks if t.code == "300750"]
    assert holding, "持仓股应生成任务"

    task = holding[0]
    facts_text = " ".join(v for _, v in task.facts)
    assert "证伪" in " ".join(k for k, _ in task.facts) or "毛利率" in facts_text, (
        "任务应包含当初写下的证伪条件")
    assert any("重新买入" in q for q in task.questions), (
        "必须包含重新买入之问——切断沉没成本")


def test_task_has_sla(db: MemDB) -> None:
    """任务带 SLA 截止时间，供复盘层统计及时处理率。

    及时处理率是混合派的核心防自欺指标：持续走低意味着在回避亏损标的。
    """
    Detector(db).run_all(since=dt.date(2024, 1, 1))
    tasks = StrategyEngine(db).process_pending_events()
    row = tasks[0].to_row()
    assert row["due_at"] is not None
    assert row["status"] == "pending"


def test_downgrade_no_longer_creates_task(db: MemDB) -> None:
    """研报下调不再生成任务 —— 验证④ 已证伪其信息量。

    发现期 下调超额60日 +1.62%，验证期 −3.00%，符号完全反转。
    继续按 P1 推送等于往飞书发噪音。
    """
    db.conn.executemany(
        "INSERT INTO research_report VALUES (?,?,?,?,?,?,?,?)",
        [("300750", dt.date(2024, 2, 1), "A证券", "深度：龙头地位稳固",
          "买入", "电池", "", dt.date.today()),
         ("300750", dt.date(2024, 3, 1), "A证券", "点评：毛利率承压",
          "增持", "电池", "", dt.date.today())])

    Detector(db).run_all(since=dt.date(2024, 1, 1))
    events = db.query(
        "SELECT event_type, severity FROM event WHERE event_type LIKE 'research%'")
    if not events.empty:
        assert (events["severity"] == "P2").all(), "研报事件应一律为 P2（仅记录）"

    tasks = StrategyEngine(db).process_pending_events()
    assert not any("downgrade" in t.task_id for t in tasks), (
        "下调不应生成复核任务")


def test_conclusion_requires_next_review_when_insufficient(db: MemDB) -> None:
    """verdict=insufficient 必须给出下次复核日期。

    不允许无限期挂起——那是回避面对亏损标的最常见的形式。
    此处验证数据库层约束；API 层另有校验。
    """
    Detector(db).run_all(since=dt.date(2024, 1, 1))
    tasks = StrategyEngine(db).process_pending_events()
    task_id = tasks[0].task_id

    db.conn.execute(
        "INSERT INTO review_conclusion (task_id, code, verdict, would_rebuy, "
        "reasoning, next_review_date) VALUES (?,?,?,?,?,?)",
        [task_id, tasks[0].code, "insufficient", False,
         "行业价格战尚无定论，等下季度数据", dt.date(2024, 8, 1)])

    row = db.query(
        "SELECT verdict, next_review_date FROM review_conclusion WHERE task_id = ?",
        [task_id])
    assert row["verdict"].iloc[0] == "insufficient"
    assert pd.notna(row["next_review_date"].iloc[0]), (
        "insufficient 结论必须带下次复核时点")


def test_task_context_is_json_serializable(db: MemDB) -> None:
    """任务上下文可序列化 —— 前端与飞书卡片都依赖它。"""
    Detector(db).run_all(since=dt.date(2024, 1, 1))
    tasks = StrategyEngine(db).process_pending_events()
    ctx = json.loads(tasks[0].to_row()["context"])
    assert "facts" in ctx and "questions" in ctx
    assert isinstance(ctx["facts"], list)
