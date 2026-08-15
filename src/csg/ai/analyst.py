"""调用 Claude 做信息处理，产出符合契约的结构化分析。

**定位**：AI 在这条链路里做的是信息处理与质疑，不是判断。
输出契约见 contract.py —— schema 上没有放结论的位置。

调用方式二选一：
- `claude -p`（无头模式），本机已安装即可用，无需 API key
- Anthropic API，需 ANTHROPIC_API_KEY

数据流：

    事件 → 组装上下文（财报/研报/行情/当初的假设与证伪条件）
         → Claude 处理
         → 契约校验（越界即拒绝）
         → 飞书卡片
         → 人做判断，CLI 提交结论
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field

from csg.ai.contract import ANALYSIS_SCHEMA, SYSTEM_PROMPT, validate_output

log = logging.getLogger(__name__)


@dataclass
class AnalysisContext:
    """喂给 Claude 的上下文。

    **数字全部来自数据库**，不让模型自行计算或回忆——
    模型对财务数字的幻觉极难通过检查输出发现。
    """

    code: str
    name: str
    event_type: str
    event_title: str

    thesis: str = ""                       # 当初的买入理由
    core_assumptions: str = ""             # 核心假设
    falsification: str = ""                # 证伪条件（；分隔）

    financials: list[dict] = field(default_factory=list)   # 已披露财报摘要
    price_summary: dict = field(default_factory=dict)      # 区间涨跌与超额
    recent_reports: list[dict] = field(default_factory=list)  # 近期研报
    flags: list[str] = field(default_factory=list)         # 命中的财务红旗

    def to_prompt(self) -> str:
        parts = [
            f"# 标的\n{self.code} {self.name}",
            f"\n# 触发事件\n类型：{self.event_type}\n{self.event_title}",
        ]

        if self.thesis or self.core_assumptions or self.falsification:
            parts.append("\n# 当初的判断（用户自己写的，请据此核对与质疑）")
            if self.thesis:
                parts.append(f"买入理由：{self.thesis}")
            if self.core_assumptions:
                parts.append(f"核心假设：{self.core_assumptions}")
            if self.falsification:
                parts.append("证伪条件：")
                for i, c in enumerate(self.falsification.split("；"), 1):
                    if c.strip():
                        parts.append(f"  {i}. {c.strip()}")

        if self.financials:
            parts.append("\n# 已披露财务数据（数字仅可引用，不可自行推算）")
            parts.append(json.dumps(self.financials, ensure_ascii=False, indent=2))

        if self.price_summary:
            parts.append("\n# 价格表现")
            parts.append(json.dumps(self.price_summary, ensure_ascii=False, indent=2))

        if self.flags:
            parts.append(f"\n# 命中的财务红旗\n{', '.join(self.flags)}")

        if self.recent_reports:
            parts.append("\n# 近期研报（当数据源用，不采信其评级结论）")
            parts.append(json.dumps(self.recent_reports, ensure_ascii=False, indent=2))

        parts.append(
            "\n# 输出要求\n"
            "严格按以下 JSON schema 输出，**只输出 JSON，不要任何额外文字**：\n"
            + json.dumps(ANALYSIS_SCHEMA, ensure_ascii=False, indent=2)
        )
        return "\n".join(parts)


class AnalystError(RuntimeError):
    pass


def _extract_json(text: str) -> dict:
    """从模型输出中抽取 JSON。

    容忍代码块包裹与前后说明文字——但不容忍缺失，
    解析失败即报错，不做「尽力而为」的降级。
    """
    text = text.strip()
    if "```" in text:
        blocks = [b for b in text.split("```") if b.strip()]
        for b in blocks:
            b = b.removeprefix("json").strip()
            if b.startswith("{"):
                text = b
                break

    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise AnalystError(f"输出中未找到 JSON：{text[:200]}")

    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise AnalystError(f"JSON 解析失败: {exc}") from exc


def call_claude_cli(prompt: str, *, timeout: int = 180) -> str:
    """通过 `claude -p` 无头模式调用。

    定时任务场景下无需 API key —— 复用本机已登录的 CLI。
    """
    try:
        proc = subprocess.run(
            ["claude", "-p", "--append-system-prompt", SYSTEM_PROMPT],
            input=prompt, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise AnalystError("未找到 claude CLI，请确认已安装") from exc
    except subprocess.TimeoutExpired as exc:
        raise AnalystError(f"claude CLI 超时（{timeout}s）") from exc

    if proc.returncode != 0:
        raise AnalystError(f"claude CLI 退出码 {proc.returncode}: {proc.stderr[:300]}")
    return proc.stdout


def call_anthropic_api(prompt: str, *, model: str = "claude-opus-5") -> str:
    """通过 Anthropic API 调用。需要 ANTHROPIC_API_KEY。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise AnalystError("未设置 ANTHROPIC_API_KEY")

    try:
        import anthropic
    except ImportError as exc:
        raise AnalystError("未安装 anthropic 包：uv add anthropic") from exc

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model, max_tokens=4096, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def analyze(
    ctx: AnalysisContext,
    *,
    backend: str = "cli",
    strict: bool = True,
) -> dict:
    """执行分析并校验契约。

    `strict=True` 时契约越界直接抛错，而非降级放行。

    这个选择是刻意的：一旦允许越界结果流向飞书，用户就会开始接收
    AI 的结论，而那正是本设计要防止的事。宁可这条提醒发不出去，
    也不要发出一条越界的。
    """
    prompt = ctx.to_prompt()
    raw = call_claude_cli(prompt) if backend == "cli" else call_anthropic_api(prompt)
    data = _extract_json(raw)

    problems = validate_output(data)
    if problems:
        msg = "AI 输出违反契约：\n" + "\n".join(f"  - {p}" for p in problems)
        if strict:
            raise AnalystError(msg)
        log.warning(msg)

    return data


def to_feishu_sections(data: dict) -> list[tuple[str, list[str]]]:
    """契约输出 → 飞书卡片分区。

    顺序刻意如此：事实在前、待你回答在后。
    「缺口」紧邻「待你回答」，让人先看到系统答不上来的部分，
    再进入自己的判断。
    """
    sections: list[tuple[str, list[str]]] = []

    if facts := data.get("facts"):
        sections.append(("📊 事实", [
            f"{f['label']}：{f['value']}　*{f['source']}*" for f in facts
        ]))

    if checks := data.get("falsification_check"):
        icon = {"triggered": "⚠️ 已触发", "not_triggered": "✅ 未触发",
                "needs_human": "❓ 需人工判断"}
        sections.append(("🔍 证伪条件核对", [
            f"{icon.get(c['status'], '?')}　{c['condition']}\n　　{c['evidence']}"
            for c in checks
        ]))

    if counters := data.get("counter_arguments"):
        sections.append(("⚔️ 反方论证", [
            f"针对「{c['target_assumption']}」\n　　{c['challenge']}"
            f"\n　　坐实条件：{c['what_would_confirm']}" for c in counters
        ]))

    if gaps := data.get("knowledge_gaps"):
        sections.append(("🕳 认知缺口（系统答不上来的）", [
            f"{g['question']}\n　　{g['why_unanswerable']}" for g in gaps
        ]))

    if questions := data.get("questions_for_you"):
        sections.append(("🤔 待你判断", list(questions)))

    return sections
