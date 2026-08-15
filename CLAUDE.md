# CSG — 个人 A 股投研系统

## 这是什么

辅助个人价值投资决策的工具。**系统整理事实并盯着人别自欺，判断永远由人做。**

不做：预测股价、宏观择时、推荐买入、自动下单。

完整方法论见 [docs/METHODOLOGY.md](docs/METHODOLOGY.md)，架构见
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，数据源实测坑见
[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)。

---

## 改代码前必须知道的五条

### 1. Point-in-Time 是最高优先级

财务数据一律以 `disclosure_date` 过滤，**绝不用 `report_period`**。
2024Q3 的报告期数据可能到 2024-10-28 才公布；在此之前使用即未来函数。

这类错误**不会报错**，只会让回测收益凭空变好且无法从结果看出。
用 `db.pit_financials()` / `db.pit_universe()`，不要手写 SQL。

### 2. 静默失效是本项目最危险的故障

开发期已撞三次，模式完全相同——**错误从不抛出，只表现为「什么都没发生」**：

| 事故 | 表现 | 代价 |
|---|---|---|
| akshare 请求挂起 | 进程活着，85 分钟消耗 3 秒 CPU | 两次采集报废 |
| baostock 会话失效 | 返回空结果集，统计成 skipped 而非 failed | 341/382 静默失败 |
| 日志管道缓冲 | 看起来卡死，实际在跑 | 误判误杀进程 |

由此得出的编码纪律：

- **任何「返回结果对象而非抛异常」的 API，必须显式检查其状态字段**
- 统计口径中 `skipped` 与 `failed` 必须能区分
- 长任务必须有可观测的进度输出（`python -u` + `grep --line-buffered`）
- 网络请求必须有超时（已在 `sources/base.py` 全局注入）

### 3. 复权只存原始价 + 因子

前复权价会因**未来的**除权事件改变历史值，一旦落盘即为错误数据。
计算收益一律用后复权（`close * adj_factor`）。

两个数据源的复权算法不一致，**同一只股票不可混用东财与 baostock**，
否则拼接处会出现虚假价格跳变。

### 4. 幸存者偏差

回测的股票池必须用 `pit_universe(as_of)`，它包含「当时在市、此后退市」的公司。
用今天的股票列表回测历史会自动剔除退市股，凭空美化收益。

### 5. DuckDB 单写锁

采集运行期间，**连只读连接都无法建立**。这是选型代价不是 bug：
API 返回 503，前端显示「数据更新中」。不要试图绕过。

内存上限 4 GB（机器仅 8 GB），**所有聚合必须下推到 SQL**，
pandas 只处理结果集。禁止 `SELECT *` 后在 pandas 里做全量计算。

---

## 常用命令

```bash
# 采集（幂等，可随时 Ctrl+C，重跑自动续传）
uv run csg sync basic          # 股票列表（含退市）+ 行业
uv run csg sync financials     # 三大报表（含披露日）
uv run csg sync research       # 研报 + 盈利预测
uv run csg sync quotes         # 行情（东财失败自动切 baostock）
uv run csg sync retry <数据集> # 定向重试失败条目

# 查询
uv run csg health              # 数据水位（陈旧 = 调度已静默失效）
uv run csg pool                # L1 股票池
uv run csg flags --as-of DATE  # 指定时点的财务红旗（严格 PIT）

# 事件驱动闭环
uv run csg ev detect           # 扫描产生事件（幂等去重）
uv run csg ev build            # 事件 → 复核任务
uv run csg ev notify           # 推送飞书（默认 dry-run）
uv run csg ev review <task_id> --verdict ... --rebuy/--no-rebuy

# 验证
uv run csg validate research   # 研报预测力（结果自动落库）
uv run csg validate runs       # 历史验证记录
uv run csg validate conclude   # 记录人工判定（adopted 仅限两期同向）

# 服务
uv run uvicorn csg.api:app --port 8000
cd web && npm run dev          # http://localhost:5173
```

---

## 目录

```
src/csg/
├── storage/     DuckDB + PIT 查询 API（schema.sql / schema_events.sql）
├── sources/     akshare（财报/研报）+ baostock（行情/PE）+ 容错层
├── pipeline/    幂等自愈采集（水位 / 续传 / 失败隔离）
├── analysis/    累计转单季 → TTM → 比率 → 红旗规则
├── events/      检测器 + 决策策略（事件 → 复核任务）
├── ai/          AI 契约（schema 上无结论字段）+ 调用
├── validation/  四个验证 + 结果持久化
├── notify/      飞书卡片
└── api/         FastAPI（只暴露，不重新实现逻辑）

web/src/         React + Vite 前端
config/          规则配置（纳入 git，改动即版本）
docs/            方法论 / 架构 / 数据源手册
```

---

## AI 参与的边界

AI 在本系统中是**信息处理器与质疑者**，不是决策者。见 [docs/AI_ASSIST.md](docs/AI_ASSIST.md)。

输出契约（`src/csg/ai/contract.py`）在 **schema 层面**没有
`verdict` / `recommendation` / `conclusion` 字段，且 `additionalProperties: false`。
不靠提示词约束——提示词会被绕过、遗忘、在多轮中漂移，结构不会。

五个部分：事实 / 证伪核对 / **反方论证** / **认知缺口** / 待你回答。
后两项不允许为空：反方论证是 AI 在此链路的最高价值项（人天然寻找
支持自己的证据）；认知缺口的价值在于诚实说出「这些你还不知道」。

---

## 尚未完成

- tushare 积分为 0，所有接口返回 `40203`，待充值后作为权威基准接入
- 验证①②③ 待跑（依赖财务数据补全）
- 事件驱动闭环端到端未验证
- `claude -p` 定时任务未接入
