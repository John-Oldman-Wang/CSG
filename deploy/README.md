# 部署：定时调度

## 为什么是 launchd 而不是 cron

目标环境是一台**会合盖休眠**的 MacBook。cron 在休眠期间会**整个跳过**
那次任务且不留任何痕迹；launchd 的 `StartCalendarInterval` 会在唤醒后
补跑一次。

这个区别在本项目里是决定性的：采集不跑 → 无新事件 → 无复核任务 →
飞书安静，而「飞书安静」恰好也是一切正常时的表现。用 cron 意味着
合盖过夜就断一天，且你无从发现。

## 两个条目，为什么必须分开

| 条目 | 时间 | 作用 |
|---|---|---|
| `com.csg.daily` | 07:30 / 19:30 | 采集 → 检测 → 建任务 → 推送 |
| `com.csg.watchdog` | 12:00 | 检查每日链是否还活着，停摆超 36h 推 P1 |

看门狗**独立于每日链**运行。由被监控者自己上报健康状态等于没有监控——
每日链若整个不启动，它连"我失败了"都发不出来。

时间选择：A 股财报多在盘后与夜间公告，07:30 抓昨夜披露；
19:30 抓当日盘后研报与行情。

## 安装

```bash
cp deploy/com.csg.*.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.csg.daily.plist
launchctl load ~/Library/LaunchAgents/com.csg.watchdog.plist
launchctl list | grep csg      # 确认已注册
```

## 验证

```bash
launchctl start com.csg.daily          # 立即手动触发一次
tail -f logs/daily_*.log               # 看进度（勿加管道，会缓冲）
.venv/bin/python scripts/watchdog.py   # 手动跑看门狗，退出码 0=健康
```

## 卸载

```bash
launchctl unload ~/Library/LaunchAgents/com.csg.daily.plist
launchctl unload ~/Library/LaunchAgents/com.csg.watchdog.plist
```

## 做不到的事

机器**关机或长期休眠**时两个条目都不运行，无法从这台机器内部发现
「这台机器没开机」。launchd 唤醒后补跑可覆盖一次合盖过夜；
连续关机数日只能靠你自己注意到。真要解决需要一台常开的机器，
超出当前部署形态（见 ARCHITECTURE.md 4.4）。

采集期间 DuckDB 写锁独占，API 返回 503、前端显示「数据更新中」。
这是选型代价不是 bug，不要试图绕过（CLAUDE.md 第 5 条）。
