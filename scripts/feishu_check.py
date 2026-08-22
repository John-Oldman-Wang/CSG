#!/usr/bin/env python3
"""飞书 Webhook 连通性自检。

刻意只用标准库，不依赖 pyyaml / requests —— 这样即使项目虚拟环境损坏、
依赖装坏或 Python 版本出问题，仍然能验证提醒通道本身是否正常。
定位是「最后一道排查手段」，正式的通知模块见 csg/notify/。

用法:
    python3 scripts/feishu_check.py            # 检查两个通道
    python3 scripts/feishu_check.py p0         # 只检查 P0
"""

import base64
import hashlib
import hmac
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SECRETS = Path(__file__).resolve().parent.parent / "config" / "secrets.yaml"
TIMEOUT = 15


def load_feishu_config(path: Path) -> dict:
    """从 secrets.yaml 抽取飞书配置。

    用正则而非 yaml 解析器，是为了保持零依赖。仅适用于本项目
    secrets.yaml 的固定两层结构，不是通用 YAML 解析。
    """
    if not path.exists():
        sys.exit(f"找不到 {path}，请先从 secrets.example.yaml 复制并填写")

    text = path.read_text(encoding="utf-8")
    feishu = re.search(r"^feishu:\s*$(.*?)(?=^\S|\Z)", text, re.MULTILINE | re.DOTALL)
    if not feishu:
        sys.exit("secrets.yaml 中没有 feishu: 配置段")

    channels: dict[str, dict[str, str]] = {}
    current = None
    for line in feishu.group(1).splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if m := re.match(r"^  (\w+):\s*$", line):
            current = m.group(1)
            channels[current] = {}
        elif m := re.match(r'^    (\w+):\s*"?([^"#]*)"?', line):
            if current:
                channels[current][m.group(1)] = m.group(2).strip()
    return channels


def sign(timestamp: str, secret: str) -> str:
    """飞书自定义机器人签名：以 "{timestamp}\\n{secret}" 为 HMAC-SHA256 密钥，
    对空消息体计算摘要后 base64 编码。timestamp 需在 1 小时内有效。
    """
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def send(webhook: str, secret: str, text: str) -> tuple[bool, str]:
    payload: dict[str, object] = {"msg_type": "text", "content": {"text": text}}
    if secret:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = sign(ts, secret)

    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        return False, f"网络错误: {e}"
    except json.JSONDecodeError as e:
        return False, f"响应非 JSON: {e}"

    # 飞书成功时返回 code 0；失败时 code 非 0 且带 msg
    if body.get("code") in (0, None) and body.get("StatusCode", 0) == 0:
        return True, "OK"
    return False, f"code={body.get('code')} msg={body.get('msg')}"


def main() -> int:
    channels = load_feishu_config(SECRETS)
    targets = sys.argv[1:] or sorted(channels)

    failed = 0
    for name in targets:
        cfg = channels.get(name)
        if not cfg or not cfg.get("webhook"):
            print(f"[{name}] 跳过：未配置 webhook")
            failed += 1
            continue

        signed = "已签名" if cfg.get("secret") else "未签名"
        ok, detail = send(
            cfg["webhook"],
            cfg.get("secret", ""),
            f"[CSG] {name.upper()} 通道连通性自检\n"
            f"若你看到这条消息，说明 {name.upper()} 提醒通道工作正常。",
        )
        status = "✓" if ok else "✗"
        print(f"[{name}] {status} {signed} — {detail}")
        failed += not ok

    if failed:
        print(f"\n{failed} 个通道异常。常见原因：签名密钥不匹配、机器人被移出群、URL 已失效。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
