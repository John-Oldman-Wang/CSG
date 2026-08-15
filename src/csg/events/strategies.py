"""决策策略：事件 → 复核任务。

**策略产出的是任务，不是建议。**

    量化系统：事件 → 信号 → 下单
    CSG     ：事件 → 任务(材料+问题) → 人判断 → 结论落库

每个策略负责三件事：
1. 判断该事件是否值得打扰你（多数事件不值得）
2. 把判断所需的材料备齐（系统的活）
3. 提出必须回答的问题（人的活）

策略绝不输出「建议买入/卖出」。见 METHODOLOGY 原则 1。

**过滤比触发更重要。** 提醒过载会导致渠道被静音，那等于系统失效。
因此默认只对观察池与持仓产生任务——对不关心的股票发提醒是
最快的自我失效路径。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import ClassVar

import pandas as pd

from csg.storage import Database

log = logging.getLogger(__name__)

# SLA：从任务创建到应处理完成的时限。
# 依据 ARCHITECTURE.md 4.4——电脑不常开，P0 的实际语义是
# 「下次联网后尽快」，而非实时。
SLA_HOURS = {"P0": 4, "P1": 24, "P2": 168}


@dataclass
class Task:
    task_id: str
    event_id: str
    code: str
    task_type: str
    severity: str
    title: str
    facts: list[tuple[str, str]] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)

    def to_row(self) -> dict:
        return {
            "task_id": self.task_id,
            "event_id": self.event_id,
            "code": self.code,
            "task_type": self.task_type,
            "severity": self.severity,
            "title": self.title,
            "context": json.dumps(
                {"facts": self.facts, "questions": self.questions},
                ensure_ascii=False),
            "status": "pending",
            "due_at": dt.datetime.now() + dt.timedelta(
                hours=SLA_HOURS.get(self.severity, 24)),
        }


def _task_id(event_id: str, task_type: str) -> str:
    return hashlib.sha1(f"{event_id}|{task_type}".encode()).hexdigest()[:16]


# 复核环节的强制问题（METHODOLOGY ⑥ 规则 5）。
# 作用是切断沉没成本：把「我亏了 20% 要不要割」重构为
# 「这是不是我今天愿意持有的资产」。
REBUY_QUESTION = "以今天的价格、今天掌握的信息，我会重新买入吗？若否，继续持有的理由是什么？"


class StrategyEngine:
    """把事件转换为任务。"""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._watchlist: pd.DataFrame | None = None

    def watchlist(self) -> pd.DataFrame:
        if self._watchlist is None:
            self._watchlist = self.db.query("SELECT * FROM watchlist")
        return self._watchlist

    def _entry(self, code: str) -> dict | None:
        wl = self.watchlist()
        hit = wl[wl["code"] == code]
        return None if hit.empty else hit.iloc[0].to_dict()

    def _stock_name(self, code: str) -> str:
        df = self.db.query("SELECT name FROM stock_basic WHERE code = ?", [code])
        return "" if df.empty else str(df["name"].iloc[0])

    # ------------------------------------------------------------------
    # 策略实现
    # ------------------------------------------------------------------

    def on_research_downgrade(self, ev: pd.Series) -> Task | None:
        """持仓/观察池标的被下调评级。

        下调之所以单独成策略：评级绝对水平几乎无信息量（买入占 94%），
        但下调逆着分析师的激励机制——不愿得罪覆盖对象却仍下调，
        通常意味着问题已较明确。

        ⚠️ 该判断为**先验假设**，正由验证④ 检验中。
        若验证期不成立，应下调本策略的严重度或直接关闭。
        """
        entry = self._entry(ev["code"])
        if entry is None:
            return None

        p = json.loads(ev["payload"]) if ev["payload"] else {}
        is_holding = entry.get("tier") == "holding"

        facts = [
            ("评级变动", f"{p.get('prev_rating')} → {p.get('rating')}（{p.get('institution')}）"),
            ("报告标题", str(p.get("report_title", ""))[:60]),
            ("持仓状态", "持仓中" if is_holding else "观察池"),
        ]
        if entry.get("core_assumptions"):
            facts.append(("当初的核心假设", entry["core_assumptions"]))
        if entry.get("falsification"):
            facts.append(("证伪条件", entry["falsification"]))

        questions = [
            "这次下调的理由，是否触及你当初写下的任何一条证伪条件？",
            "下调依据的是新信息，还是对已知信息的重新解读？（后者信息量低）",
        ]
        if is_holding:
            questions.append(REBUY_QUESTION)

        return Task(
            task_id=_task_id(ev["event_id"], "downgrade_review"),
            event_id=ev["event_id"],
            code=ev["code"],
            task_type="holding_review" if is_holding else "watchlist_alert",
            severity="P1" if is_holding else "P2",
            title=ev["title"],
            facts=facts,
            questions=questions,
        )

    def on_report_disclosed(self, ev: pd.Series) -> Task | None:
        """财报披露 → 逐条核对证伪条件。

        监控的对象是**假设**，不是价格（METHODOLOGY ⑤）。
        """
        entry = self._entry(ev["code"])
        if entry is None:
            return None

        p = json.loads(ev["payload"]) if ev["payload"] else {}
        facts = [("报告期", str(p.get("report_period", "")))]
        if p.get("revenue") is not None:
            facts.append(("营业总收入", f"{p['revenue'] / 1e8:.2f} 亿"))
        if p.get("net_profit") is not None:
            facts.append(("归母净利润", f"{p['net_profit'] / 1e8:.2f} 亿"))
        if entry.get("core_assumptions"):
            facts.append(("核心假设", entry["core_assumptions"]))
        if entry.get("falsification"):
            facts.append(("证伪条件", entry["falsification"]))

        return Task(
            task_id=_task_id(ev["event_id"], "falsification_check"),
            event_id=ev["event_id"],
            code=ev["code"],
            task_type="holding_review",
            severity="P1",
            title=f"{ev['title']} —— 核对证伪条件",
            facts=facts,
            questions=[
                "逐条对照证伪条件：有哪条被触发了？",
                "本期数据是否削弱了核心假设？削弱到什么程度？",
                REBUY_QUESTION,
            ],
        )

    def on_excess_drawdown(self, ev: pd.Series) -> Task | None:
        """超额下跌 → 复核任务（混合派的核心动作）。

        **价格下跌不产生任何买卖信号**，只触发调查。
        跌幅决定紧急程度，不决定动作方向（METHODOLOGY ⑥ 规则 1）。
        """
        entry = self._entry(ev["code"])
        if entry is None:
            return None

        p = json.loads(ev["payload"]) if ev["payload"] else {}
        facts = [
            ("个股区间涨跌", f"{p.get('stock_return', 0):.1%}"),
            ("同行业中位", f"{p.get('industry_return', 0):.1%}"),
            ("超额部分", f"{p.get('excess', 0):.1%} ← 这部分才是个股特有的"),
        ]
        if entry.get("thesis"):
            facts.append(("当初的买入理由", entry["thesis"]))
        if entry.get("falsification"):
            facts.append(("证伪条件", entry["falsification"]))

        return Task(
            task_id=_task_id(ev["event_id"], "drawdown_review"),
            event_id=ev["event_id"],
            code=ev["code"],
            task_type="holding_review",
            severity="P1",
            title=ev["title"],
            facts=facts,
            questions=[
                "这是情绪面还是价值面？依据是什么？",
                "期间是否出现了触及证伪条件的新信息？",
                REBUY_QUESTION,
                "若判定为情绪面：你打算加仓吗？加仓后是否突破单票上限？",
            ],
        )

    # ------------------------------------------------------------------

    _HANDLERS: ClassVar[dict[str, str]] = {
        "report_disclosed": "on_report_disclosed",
        "excess_drawdown": "on_excess_drawdown",
        # research_downgrade 已移除 —— 验证④ 证伪了「下调有信息量」的先验：
        #     发现期 下调超额60日 +1.62%、250日 +8.09%
        #     验证期 下调超额60日 −3.00%、250日 −10.72%
        # 符号在两期完全反转，属噪音。若仅看发现期就接入决策链路，
        # 等于连续数年向用户推送噪音——样本内外分割在此处兑现了价值。
        #
        # research_upgrade / coverage_surge 同样不生成任务：
        # 前者验证期虽为正但发现期为负，同样不可复现；
        # 后者各期无稳定规律。三者均仅记录事件供样本积累。
    }

    def process_pending_events(self, *, limit: int = 200) -> list[Task]:
        """把尚未生成任务的事件转换为任务。

        幂等：task_id 由 (event_id, task_type) 决定，重复处理不产生重复任务。
        """
        events = self.db.query(
            """
            SELECT e.* FROM event e
            WHERE e.is_backfill = FALSE
              AND NOT EXISTS (SELECT 1 FROM review_task t WHERE t.event_id = e.event_id)
            ORDER BY e.ref_date DESC
            LIMIT ?
            """,
            [limit],
        )
        if events.empty:
            return []

        tasks: list[Task] = []
        for _, ev in events.iterrows():
            handler_name = self._HANDLERS.get(ev["event_type"])
            if not handler_name:
                continue
            try:
                task = getattr(self, handler_name)(ev)
                if task is not None:
                    tasks.append(task)
            except Exception as exc:  # noqa: BLE001 — 单事件失败不应中断整批
                log.warning("事件 %s 处理失败: %s", ev["event_id"], exc)

        if tasks:
            self.db.upsert("review_task",
                           pd.DataFrame([t.to_row() for t in tasks]), ["task_id"])
        return tasks
