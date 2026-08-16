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

---

## ⚠️ 事故：launchd 被 macOS TCC 拦住（2026-08-16 首次触发即失败）

首次自动运行的结果：

```
com.csg.daily: runs = 1, last exit code = 126
logs/launchd_daily.err:
  shell-init: getcwd: cannot access parent directories: Operation not permitted
  /bin/bash: .../scripts/daily_chain.sh: Operation not permitted
```

**原因不是脚本、权限位或路径**（`chmod +x` 已加，手动执行正常）。
macOS 对 `~/Documents`、`~/Desktop`、`~/Downloads` 有 TCC 隐私保护，
**launchd 启动的进程默认无权访问**。本仓库位于 `~/Documents/GitHub/CSG`，
于是 launchd 连脚本都读不到，退出码 126。

**看门狗同样失效**：它也在 `~/Documents` 下，12:00 触发时会以同样方式挂掉。
即：专门用来发现静默失效的东西，自己正在静默失效。这类「监控与被监控者
共享同一失效模式」的问题，唯一的防法是让监控依赖更少的东西——
而 TCC 是进程级的，同一目录下做不到。

### 三个修法

**方案 A：只给项目自己的 venv python 授权**（推荐，不动目录）

前提已在代码侧做好：整条链已收进 `csg daily` 命令，
plist 直接调 `.venv/bin/python -m csg.cli daily`，**不再经过 `/bin/bash`**。
看门狗本就调同一个解释器，故只需授权一次即可覆盖两个任务。

    系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 「+」
    → Command+Shift+G → 粘贴：
      /Users/wwww/Documents/GitHub/CSG/.venv/bin/python
    → 添加并打开开关

授权面就是这个项目专属的解释器——它只被本项目使用，
爆炸半径仅限本项目。**不要给 `/bin/bash` 授权**：那会让此后
任何 bash 脚本（包括你无意中执行的）都获得完全磁盘访问权。

⚠️ 重建 venv（`uv sync --reinstall` 等）会替换该二进制文件，
TCC 授权按路径+签名记录，**重建后需重新授权**。届时症状同样是
exit 126，按本节排查。

**方案 B：授予 `/bin/bash` 完全磁盘访问权限**（不推荐，见上）

**方案 C：把仓库移出受保护目录**（一劳永逸，但要动目录）

```bash
mkdir -p ~/Projects && mv ~/Documents/GitHub/CSG ~/Projects/CSG
cd ~/Projects/CSG && sed -i '' 's|/Users/wwww/Documents/GitHub/CSG|/Users/wwww/Projects/CSG|g' \
  deploy/com.csg.*.plist scripts/daily_chain.sh
cp deploy/com.csg.*.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.csg.daily.plist
launchctl unload ~/Library/LaunchAgents/com.csg.watchdog.plist
launchctl load ~/Library/LaunchAgents/com.csg.daily.plist
launchctl load ~/Library/LaunchAgents/com.csg.watchdog.plist
```

`~/Projects` 不在 TCC 保护范围内，此后不再需要任何授权。
git remote 不受影响；`.claude/launch.json` 若有绝对路径需一并改。

### 验证修好了

```bash
launchctl start com.csg.daily
sleep 20 && cat logs/launchd_daily.err     # 应为空
tail -f logs/daily_*.log                   # 应有内容
launchctl print gui/$(id -u)/com.csg.daily | grep 'last exit code'   # 应为 0
```

**exit code 126 = 找到了但无法执行**，几乎总是 TCC；
**127 = 找不到**，那才是路径问题。两者不要混淆。
