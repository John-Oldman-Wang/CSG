# pipeline — 采集管线

## 做什么

幂等自愈的数据采集。**目标环境是一台会合盖休眠、会断网的 MacBook，
且数据源会限流——采集必然中断。**

## 怎么做

不写「跑今天的数据」式任务，一律按：

```
查本地水位 → 与目标区间比对 → 只补缺口 → 每单元成功即落水位
```

水位粒度是**单只股票**，因此：
- 中断后重跑只补未完成的部分
- 单只失败被隔离，不影响其余
- 可以随时 Ctrl+C，零成本

```python
ing.sync_stock_basic()    # 股票列表（含退市股，消除幸存者偏差）
ing.sync_daily_quotes()   # 行情，东财失败自动切 baostock
ing.sync_financials()     # 三大报表 + 披露日期
ing.sync_research()       # 研报 + 盈利预测（保留快照）
ing.sync_valuations()     # PE/PB/PS
ing.health_report()       # 水位陈旧检测 ← 调度静默失效的唯一发现手段
```

## 坑

**`skipped` 与 `failed` 必须能区分。** 曾因 baostock 会话失效返回空结果集
而非报错，341/382 只被统计成 skipped，日志一片干净——**假成功比失败危险**。

**日志不要经过任何管道。** 本项目四次因日志不可见而误判系统状态
（socket 挂起、requests 挂起、grep 块缓冲、日志条件写错）。
tqdm 噪音用 `TQDM_DISABLE` 从源头关闭，而非事后 grep 过滤。

**逐单元记录耗时**，只报进度不报速度时无法区分「慢」与「卡住」。
