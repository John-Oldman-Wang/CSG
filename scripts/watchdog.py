#!/usr/bin/env python
"""看门狗 —— 监控「每日链是否还活着」。

**这个脚本存在的唯一理由：区分「今天没消息」与「系统死了」。**

本项目已三次因静默失效报废工作（CLAUDE.md 第 2 条）。调度层的静默
失效尤其危险：采集不跑 → 没有新事件 → 没有复核任务 → 飞书安静。
而「飞书安静」恰好也是一切正常时的表现。两者从外部完全无法区分，
你会以为系统在盯着，其实它三天前就停了。

因此本脚本**不检查数据是否正确，只检查链条是否在跑**：
读取 daily_chain.sh 成功时写下的时间戳，超过阈值即推送 P1 告警。

它必须独立于每日链运行（独立的 launchd 条目）——由被监控者自己
上报健康状态，等于没有监控。

**做不到的事，说清楚**：机器关机或长期休眠时本脚本同样不运行，
无法从这台机器内部发现「这台机器没开机」。launchd 会在唤醒后补跑，
故一次合盖过夜可以覆盖；连续关机数日则只能靠你自己注意到。
真要解决需要一台常开的机器，超出当前部署形态（ARCHITECTURE.md 4.4）。
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

STATE = ROOT / "logs" / "last_success"
LOG_DIR = ROOT / "logs"

# 阈值取 36 小时而非 24：
# 每日链一天跑两次，正常间隔约 12 小时。36 小时意味着连续三次未成功，
# 足以排除单次网络抖动，又不至于让故障潜伏到第二天以后。
STALE_HOURS = 36


def main() -> int:
    from csg.notify.feishu import FeishuNotifier

    now = dt.datetime.now()

    if not STATE.exists():
        msg = (f"每日链从未成功执行过（{STATE} 不存在）。\n"
               f"若刚部署，等待下一次 07:30 / 19:30 的调度；\n"
               f"否则检查：launchctl list | grep csg")
        age_h = None
    else:
        # 本地时区即可：只用于计算「距上次成功多久」，不跨时区比较
        last = dt.datetime.fromtimestamp(  # noqa: DTZ006
            int(STATE.read_text().strip()))
        age_h = (now - last).total_seconds() / 3600
        if age_h < STALE_HOURS:
            print(f"[OK] 每日链 {age_h:.1f} 小时前成功，阈值 {STALE_HOURS}h")
            return 0
        msg = (f"每日链已 {age_h:.1f} 小时未成功完成"
               f"（阈值 {STALE_HOURS}h，最后成功 {last:%F %T}）。\n"
               f"这意味着财报与研报数据可能已停止更新，"
               f"而「没有推送」不再代表「没有事件」。")

    # 附上最近一次日志的尾部——告警若不带线索，只会变成又一条被忽略的通知
    logs = sorted(LOG_DIR.glob("daily_*.log"), reverse=True)
    if logs:
        tail = logs[0].read_text(errors="replace").strip().splitlines()[-12:]
        msg += f"\n\n最近日志 {logs[0].name}：\n" + "\n".join(tail)
    else:
        msg += "\n\n未找到任何日志文件——调度可能从未触发。"

    print(f"[ALERT] {msg}")
    ok, detail = FeishuNotifier().send_text("p1", f"🔴 CSG 采集链停摆\n\n{msg}")
    if not ok:
        # 告警通道本身失效是最坏情况：此时连「系统死了」都传不出去。
        #
        # 事故（2026-08-19 ~ 08-22）：本函数连续三天正确判定停摆
        # （57h / 80.8h / 105h），但通道名写成了大写 "P1" 而配置里是 "p1"，
        # 每次都只留下一行 stderr。用户最终是因为前端 503 才发现，
        # 而不是因为收到告警。
        #
        # 教训不在大小写，在**我当初"测过"告警**：测试里 monkey-patch 掉了
        # send_text，恰好绕过唯一坏掉的那一环。
        # 端到端未跑通的告警链，等于没有告警链。
        #
        # 因此除 stderr 外，再落一个文件标记：它会被 `csg health`
        # 与前端 /api/health 读到，使「告警发不出去」本身可见。
        print(f"[FATAL] 告警推送失败: {detail}", file=sys.stderr)
        try:
            (LOG_DIR / "ALERT_UNDELIVERED").write_text(
                f"{now:%F %T}\n推送失败: {detail}\n\n{msg}\n", encoding="utf-8")
        except OSError:
            pass
        return 2

    # 推送成功则清除标记
    marker = LOG_DIR / "ALERT_UNDELIVERED"
    if marker.exists():
        marker.unlink(missing_ok=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
