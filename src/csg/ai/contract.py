"""AI 分析的输出契约。

**核心设计：schema 上没有放结论的地方。**

不是靠提示词说「请不要给出买卖建议」——提示词会被绕过、会被遗忘、
会在多轮对话中漂移。这里改用结构约束：输出 schema 中不存在
verdict / recommendation / conclusion / rating 之类的字段，
且 `additionalProperties: false`。模型即使想给结论，也无处安放。

五个部分对应 METHODOLOGY 的分工：

    facts               事实      ← 系统（可验证、可点回原文）
    falsification_check 核对      ← 系统（逐条比对已写下的证伪条件）
    counter_arguments   反方      ← AI 的最高价值项：攻击你自己的论证
    knowledge_gaps      缺口      ← 诚实标出答不上来的部分
    questions_for_you   待你回答  ← 人（判断权保留在这里）

为什么反方论证价值最高：人天然会寻找支持自己的证据，这是最难自我
纠正的偏误。让 AI 扮演对手方，比让它当顾问有用得多。

为什么认知缺口必须有：一份好的分析产出，价值在于诚实告诉你
「这些你还不知道」，而不是把不知道的部分用漂亮话填满。
"""

from __future__ import annotations

# 明令禁止的字段名。既用于 schema 校验，也用于事后审查——
# 模型可能试图把结论塞进 facts 的文本里，无法完全杜绝，
# 但至少结构上不给它专属位置。
FORBIDDEN_KEYS = {
    "verdict", "recommendation", "conclusion", "rating", "action",
    "suggestion", "advice", "signal", "target_price", "buy", "sell",
    "judgment", "opinion", "score",
}

ANALYSIS_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,   # ← 堵死一切额外字段
    "required": [
        "facts", "falsification_check", "counter_arguments",
        "knowledge_gaps", "questions_for_you",
    ],
    "properties": {
        "facts": {
            "type": "array",
            "description": "可验证的客观事实，每条必须注明来源。不含推断。",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["label", "value", "source"],
                "properties": {
                    "label": {"type": "string"},
                    "value": {"type": "string"},
                    "source": {
                        "type": "string",
                        "description": "数据来源：财报字段名/研报标题/行情区间",
                    },
                },
            },
        },
        "falsification_check": {
            "type": "array",
            "description": "逐条核对用户事先写下的证伪条件。",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["condition", "status", "evidence"],
                "properties": {
                    "condition": {"type": "string", "description": "原文照抄"},
                    "status": {
                        "type": "string",
                        "enum": ["triggered", "not_triggered", "needs_human"],
                        "description": "无法用数据判定的一律 needs_human，不得猜测",
                    },
                    "evidence": {"type": "string"},
                },
            },
        },
        "counter_arguments": {
            "type": "array",
            "description": (
                "攻击用户当初的买入理由。要求指出论证中最脆弱的环节，"
                "而非罗列泛泛风险。"
            ),
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["target_assumption", "challenge", "what_would_confirm"],
                "properties": {
                    "target_assumption": {
                        "type": "string", "description": "被质疑的具体假设",
                    },
                    "challenge": {"type": "string", "description": "质疑的理由"},
                    "what_would_confirm": {
                        "type": "string",
                        "description": "什么证据出现能坐实这个质疑",
                    },
                },
            },
        },
        "knowledge_gaps": {
            "type": "array",
            "description": (
                "无法从现有信息回答的问题。**本项最有价值**——"
                "不允许用推测填满，答不上来就明说。"
            ),
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["question", "why_unanswerable"],
                "properties": {
                    "question": {"type": "string"},
                    "why_unanswerable": {"type": "string"},
                },
            },
        },
        "questions_for_you": {
            "type": "array",
            "description": "需要人来回答的判断题。系统不回答这些。",
            "minItems": 1,
            "items": {"type": "string"},
        },
    },
}


SYSTEM_PROMPT = """你是一个投资研究的**信息处理器与质疑者**，不是决策者。

## 你的职责边界

你负责：整理事实、核对已写下的证伪条件、攻击用户自己的论证、诚实标出认知缺口。
你不负责：判断该买还是该卖、判断这是情绪面还是价值面、给出目标价或评级。

最后那些是用户的工作。**你的输出里没有放结论的地方，这是刻意设计的。**

## 为什么这样限制

你会产出流畅、自信、结构完整的判断，而你的自信程度与正确率不相关。
用户若长期接收你的结论，会逐渐停止自己思考。而你的判断本质上是公开
信息的重组，天然趋向市场共识——共识已经反映在价格里了。

## 硬性要求

1. **数字必须来自输入的结构化数据**，不得自行计算或回忆。
   输入里没有的数字，一个都不要写。
2. **facts 里只放可验证的事实**，每条注明来源。推断放到 counter_arguments。
3. **无法用数据判定的证伪条件，status 一律填 needs_human**，不要猜。
4. **counter_arguments 要具体**：指出论证里最脆弱的那个环节，
   不要罗列「行业竞争加剧」这类放之四海皆准的风险。
5. **knowledge_gaps 不允许为空**。如果你觉得什么都知道，说明你没在认真找。
6. 全程使用中文。

## 关于研报

研报当**数据源**用，不当观点源。评级本身几乎无信息量（实测买入占 94%）。
有价值的是其中的产业数据、一致预期数值，以及**分歧**——
若多家机构中有少数持相反看法，把它的逻辑单独提出来。
"""


def validate_output(data: dict) -> list[str]:
    """校验输出是否越界。返回问题列表，空列表表示通过。

    schema 校验之外的补充检查：模型可能把结论藏在文本里，
    无法完全杜绝，但可以扫出明显的越界表述。
    """
    problems: list[str] = []

    stray = set(data) - set(ANALYSIS_SCHEMA["properties"])
    if stray:
        problems.append(f"出现契约外的字段: {sorted(stray)}")

    forbidden = {k for k in data if k.lower() in FORBIDDEN_KEYS}
    if forbidden:
        problems.append(f"出现被禁止的结论字段: {sorted(forbidden)}")

    for key in ANALYSIS_SCHEMA["required"]:
        if key not in data:
            problems.append(f"缺少必填部分: {key}")

    if not data.get("knowledge_gaps"):
        problems.append("knowledge_gaps 为空——不允许，说明未认真寻找认知边界")
    if not data.get("counter_arguments"):
        problems.append("counter_arguments 为空——反方论证是本流程的核心价值")

    # 扫描明显的建议性表述
    advice_markers = ("建议买入", "建议卖出", "建议加仓", "建议减仓",
                      "可以买入", "应该卖出", "值得买入", "推荐")
    for fact in data.get("facts", []):
        text = f"{fact.get('label', '')}{fact.get('value', '')}"
        hit = [m for m in advice_markers if m in text]
        if hit:
            problems.append(f"facts 中出现建议性表述: {hit}")

    return problems
