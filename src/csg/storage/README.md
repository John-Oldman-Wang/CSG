# storage — 存储层

## 做什么

DuckDB 的连接管理、schema 定义，以及 **Point-in-Time 查询 API**。

## 怎么做

```
schema.sql          数据层 12 表（基础/行情/财务/研报/管线状态）
schema_events.sql   事件层 8 表（事件/任务/结论/推送/验证结果）
db.py               连接封装 + PIT 查询 + 幂等 upsert + 水位管理
```

### PIT 查询以方法形式提供，不鼓励手写 SQL

```python
db.pit_financials(as_of, table="fin_income")   # 只返回该时点已披露的
db.pit_universe(as_of)                          # 含此后退市的公司
```

用 `report_period` 代替 `disclosure_date` 过滤，是本项目最容易犯、
最难发现的错误——它不报错，只让回测收益凭空变好。封装成方法是为了
让调用方没有机会写错。

### 内存约束决定了计算位置

目标机器 8 GB。全市场日线约 1900 万行，一次性载入 pandas 会触发 swap。
因此 DuckDB 上限设 4 GB 并启用磁盘溢写，**所有聚合必须下推到 SQL**，
pandas 只处理结果集。禁止 `SELECT *` 后在 pandas 里做全量计算。

## 坑

**upsert 必须用 `INSERT OR REPLACE`。** 早期用 `DELETE ... USING` + `INSERT`，
在带主键索引的表上会抛 FatalException 并使整个数据库连接失效，
导致采集整轮中断且无法优雅降级。

**索引可能损坏。** 一次崩溃后 `Failed to delete all rows from index` 会持续复现，
此时需重建表以重建索引。注意：只测「插入新行」发现不了这个问题——
索引删除只在**替换既有行**时才走到。

**DuckDB 是单进程写锁**，采集运行期间连只读连接都建不了。这是选型代价，
API 层返回 503 并说明原因，不要试图绕过。
