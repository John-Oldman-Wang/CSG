"""飞书机器人推送。

**推送的是「这件事发生了，材料在此，请你判断」，不是买卖建议。**
系统不输出结论（METHODOLOGY 原则 1）。卡片呈现事实与待答问题，
判断仍归人。

**分级是生死线**：P0 与 P1/P2 走不同的群。混在一起的直接后果是
用户静音整个渠道，那等于整套提醒系统失效（ARCHITECTURE.md L7）。

**能力边界**：卡片按钮的回调需要飞书服务器反向访问本机，
而 MacBook 无公网 IP，故按钮**不可用**。交互模型是
「飞书告诉你要做什么，回到电脑上用 CLI 提交结论」。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Literal

import yaml

log = logging.getLogger(__name__)

SECRETS_PATH = Path("config/secrets.yaml")
Channel = Literal["p0", "p1"]

# 卡片主题色：与严重度对应，让人在通知列表里一眼分辨
_TEMPLATE = {"P0": "red", "P1": "orange", "P2": "blue"}


def load_channels(path: Path | str = SECRETS_PATH) -> dict:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return cfg.get("feishu", {}) or {}


def _sign(timestamp: str, secret: str) -> str:
    """飞书签名：以 "{timestamp}\\n{secret}" 为 HMAC-SHA256 密钥对空体求摘要。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _post(webhook: str, secret: str, payload: dict, timeout: int = 15) -> tuple[bool, str]:
    body = dict(payload)
    if secret:
        ts = str(int(time.time()))
        body["timestamp"] = ts
        body["sign"] = _sign(ts, secret)

    req = urllib.request.Request(
        webhook,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        return False, f"网络错误: {exc}"
    except json.JSONDecodeError as exc:
        return False, f"响应非 JSON: {exc}"

    if data.get("code") in (0, None) and data.get("StatusCode", 0) == 0:
        return True, "ok"
    return False, f"code={data.get('code')} msg={data.get('msg')}"


class FeishuNotifier:
    def __init__(self, channels: dict | None = None) -> None:
        self.channels = channels if channels is not None else load_channels()

    def _channel(self, name: Channel) -> tuple[str, str]:
        cfg = self.channels.get(name) or {}
        return cfg.get("webhook", ""), cfg.get("secret", "")

    def send_text(self, channel: Channel, text: str) -> tuple[bool, str]:
        webhook, secret = self._channel(channel)
        if not webhook:
            return False, f"通道 {channel} 未配置"
        return _post(webhook, secret, {"msg_type": "text", "content": {"text": text}})

    def send_task_card(
        self,
        channel: Channel,
        *,
        severity: str,
        title: str,
        code: str,
        name: str = "",
        facts: list[tuple[str, str]],
        questions: list[str],
        task_id: str = "",
        due: str = "",
    ) -> tuple[bool, str]:
        """复核任务卡片。

        结构刻意分为「事实」与「待你回答」两块：
        事实由系统提供，问题由人回答。这条边界在界面上也要可见，
        否则久而久之会把系统整理的材料误当成系统的结论。

        **卡片不展示持仓成本与浮盈亏** —— 切断沉没成本对判断的干扰
        （METHODOLOGY ⑥ 规则 5）。
        """
        webhook, secret = self._channel(channel)
        if not webhook:
            return False, f"通道 {channel} 未配置"

        fact_lines = "\n".join(f"**{k}**：{v}" for k, v in facts)
        question_lines = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))

        elements: list[dict[str, Any]] = [
            {"tag": "div", "text": {
                "tag": "lark_md",
                "content": f"**{code} {name}**\n{title}"}},
            {"tag": "hr"},
            {"tag": "div", "text": {
                "tag": "lark_md", "content": f"**事实**\n{fact_lines}"}},
            {"tag": "hr"},
            {"tag": "div", "text": {
                "tag": "lark_md", "content": f"**待你判断**\n{question_lines}"}},
        ]

        footer = []
        if due:
            footer.append(f"处理时限 {due}")
        if task_id:
            # 无公网 IP，按钮回调不可用，故以命令行方式引导
            footer.append(f"提交结论：`csg review {task_id}`")
        if footer:
            elements.append({"tag": "note", "elements": [
                {"tag": "plain_text", "content": " · ".join(footer)}]})

        card = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text",
                              "content": f"[{severity}] {code} {name}".strip()},
                    "template": _TEMPLATE.get(severity, "blue"),
                },
                "elements": elements,
            },
        }
        return _post(webhook, secret, card)

    def send_digest(
        self, channel: Channel, title: str, sections: list[tuple[str, list[str]]]
    ) -> tuple[bool, str]:
        """汇总消息，用于 P2 周报。"""
        webhook, secret = self._channel(channel)
        if not webhook:
            return False, f"通道 {channel} 未配置"

        elements: list[dict[str, Any]] = []
        for heading, lines in sections:
            if not lines:
                continue
            if elements:
                elements.append({"tag": "hr"})
            elements.append({"tag": "div", "text": {
                "tag": "lark_md",
                "content": f"**{heading}**\n" + "\n".join(f"· {ln}" for ln in lines)}})

        if not elements:
            elements = [{"tag": "div", "text": {
                "tag": "plain_text", "content": "本期无事项"}}]

        return _post(webhook, secret, {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": title},
                           "template": "blue"},
                "elements": elements,
            },
        })


def channel_for(severity: str) -> Channel:
    """严重度到通道的映射。P0 单独成群，其余合并。"""
    return "p0" if severity == "P0" else "p1"
