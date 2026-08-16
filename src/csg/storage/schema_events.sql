-- 事件层 schema
--
-- 事件驱动的核心约束（与量化系统的根本区别）：
--
--     量化：事件 → 信号 → 自动下单
--     CSG ：事件 → 复核任务 → 人判断 → 结论落库 →（可能的）动作
--
-- 事件的终点是一个**待办任务**，不是交易信号。这是方法论决定的：
-- 下跌触发调查，不触发动作（见 METHODOLOGY.md 原则 5）。
--
-- 由此推出两个设计要求：事件不需要低延迟，但需要完整的生命周期
-- 与 SLA——更接近工单系统，而非行情引擎。

-- ============================================================
-- 事件流
-- ============================================================

-- 幂等去重是硬要求：MacBook 会休眠，唤醒后补跑扫描是常态。
-- 同一份研报、同一份财报被扫描两次不得产生两个事件，
-- 否则一次补跑就会把提醒淹没。
CREATE TABLE IF NOT EXISTS event (
    event_id      VARCHAR PRIMARY KEY,   -- 由 (type, code, ref_date, ref_key) 哈希得到
    event_type    VARCHAR NOT NULL,      -- research_downgrade / report_disclosed / price_drawdown ...
    code          VARCHAR NOT NULL,
    ref_date      DATE NOT NULL,         -- 事件所属日期（研报发布日 / 财报披露日 / 交易日）
    ref_key       VARCHAR,               -- 同日多事件的区分键（如机构名）
    severity      VARCHAR NOT NULL,      -- P0 / P1 / P2
    title         VARCHAR NOT NULL,
    payload       JSON,                  -- 事件详情，供任务展示与复核材料
    detected_at   TIMESTAMP DEFAULT current_timestamp,
    is_backfill   BOOLEAN DEFAULT FALSE  -- 历史回填产生的事件不推送
);

-- ============================================================
-- 复核任务
-- ============================================================

-- 一个事件可能不产生任务（常规财报无异常），也可能产生多个
-- （财报 + 证伪命中 + 组合超限），故与 event 分离。
--
-- SLA 存在的意义：混合派把「下跌怎么办」交给人的判断，
-- 其最大失效风险不是逻辑漏洞而是执行——任务积压不处理。
-- due_at 与状态机直接喂给复盘层的「及时处理率」统计。
CREATE TABLE IF NOT EXISTS review_task (
    task_id       VARCHAR PRIMARY KEY,
    event_id      VARCHAR NOT NULL,
    code          VARCHAR NOT NULL,
    task_type     VARCHAR NOT NULL,      -- holding_review / watchlist_alert / risk_check
    severity      VARCHAR NOT NULL,
    title         VARCHAR NOT NULL,
    context       JSON,                  -- 系统备好的判断材料
    status        VARCHAR NOT NULL DEFAULT 'pending',
                                         -- pending → notified → in_review → concluded
    created_at    TIMESTAMP DEFAULT current_timestamp,
    due_at        TIMESTAMP,             -- SLA 截止；超时升级提醒
    notified_at   TIMESTAMP,
    concluded_at  TIMESTAMP
);

-- 复核结论（METHODOLOGY ⑥ 复核环节）
--
-- verdict 三选一，「信息不足」必须填写 next_review_date，
-- 不允许无限期挂起——那是逃避面对亏损标的最常见的形式。
--
-- would_rebuy 是强制必答项：
--   「以今天的价格、今天掌握的信息，我会重新买入吗？」
-- 用于切断沉没成本。复核界面默认隐藏持仓成本与浮盈亏。
CREATE TABLE IF NOT EXISTS review_conclusion (
    task_id           VARCHAR PRIMARY KEY,
    code              VARCHAR NOT NULL,
    verdict           VARCHAR NOT NULL,  -- sentiment / fundamental / insufficient
    would_rebuy       BOOLEAN NOT NULL,
    reasoning         VARCHAR NOT NULL,
    falsified_items   VARCHAR,           -- 被证伪的假设条目
    next_review_date  DATE,              -- verdict = insufficient 时必填
    action_taken      VARCHAR,           -- none / add / reduce / exit
    concluded_at      TIMESTAMP DEFAULT current_timestamp
);

-- ============================================================
-- 推送记录
-- ============================================================

-- 去重与限频的依据。提醒过载会导致用户静音整个渠道，
-- 那等于整套系统失效（见 ARCHITECTURE.md L7）。
CREATE TABLE IF NOT EXISTS notification_log (
    notif_id      VARCHAR PRIMARY KEY,
    task_id       VARCHAR,
    event_id      VARCHAR,
    channel       VARCHAR NOT NULL,      -- feishu_p0 / feishu_p1
    sent_at       TIMESTAMP DEFAULT current_timestamp,
    success       BOOLEAN NOT NULL,
    error         VARCHAR
);

-- ============================================================
-- 持仓与观察池（策略需要知道「这只票与我有什么关系」）
-- ============================================================

CREATE TABLE IF NOT EXISTS watchlist (
    code              VARCHAR PRIMARY KEY,
    added_at          DATE NOT NULL,
    tier              VARCHAR NOT NULL,  -- watch(观察池) / holding(持仓)
    thesis            VARCHAR,           -- 买入理由
    core_assumptions  VARCHAR,           -- 核心假设
    falsification     VARCHAR,           -- 证伪条件 ← L6 监控的直接输入
    target_price      DOUBLE,
    notes             VARCHAR
);

CREATE INDEX IF NOT EXISTS idx_event_code    ON event(code, ref_date);
CREATE INDEX IF NOT EXISTS idx_event_type    ON event(event_type, ref_date);
CREATE INDEX IF NOT EXISTS idx_task_status   ON review_task(status, due_at);

-- ============================================================
-- 验证结果持久化
-- ============================================================
--
-- 回测结果必须落库而非每次重算，理由有三：
--   1. 回测耗时，且依赖当时的数据状态
--   2. 结论会被后续决策规则引用（如「评级下调」事件的权重），
--      需要可追溯：何时、用何数据、得出何结论
--   3. 参数调整后的结果需要横向对比，而非覆盖

CREATE TABLE IF NOT EXISTS validation_run (
    run_id          VARCHAR PRIMARY KEY,
    validation_type VARCHAR NOT NULL,   -- research_reliability / flag_backtest / ...
    run_at          TIMESTAMP DEFAULT current_timestamp,
    params          JSON,               -- 运行参数，用于复现
    data_snapshot   JSON,               -- 当时的数据规模与日期范围
    status          VARCHAR DEFAULT 'ok',
    note            VARCHAR
);

-- 结构化结果。一行 = 一个(视角, 样本期, 分组)的指标集合。
-- 拆成长表而非宽表：不同验证的指标集不同，宽表无法容纳。
CREATE TABLE IF NOT EXISTS validation_result (
    run_id        VARCHAR NOT NULL,
    view_name     VARCHAR NOT NULL,     -- 评级 / 评级调整 / 发布前走势 / 覆盖密度 ...
    sample_period VARCHAR NOT NULL,     -- 发现期 / 验证期
    group_key     VARCHAR NOT NULL,     -- 买入 / 下调 / 前期最强 ...
    sample_size   INTEGER,
    metrics       JSON NOT NULL,
    PRIMARY KEY (run_id, view_name, sample_period, group_key)
);

-- 人工判定：把回测发现转化为是否采纳的决策。
--
-- 这是「回测 → 决策规则」的桥梁，也是防过拟合的最后一道关：
-- verdict 只有在发现期与验证期**同向**时才允许 adopted。
-- 仅在发现期成立的一律 rejected，且保留记录——
-- 记住哪些想法被证伪过，与记住哪些成立同样重要。
CREATE TABLE IF NOT EXISTS validation_conclusion (
    conclusion_id   VARCHAR PRIMARY KEY,
    run_id          VARCHAR,
    validation_type VARCHAR NOT NULL,
    finding         VARCHAR NOT NULL,   -- 发现的效应描述
    in_sample       VARCHAR,            -- 发现期结果摘要
    out_sample      VARCHAR,            -- 验证期结果摘要
    verdict         VARCHAR NOT NULL,   -- adopted / rejected / pending
    applied_to      VARCHAR,            -- 应用于哪条决策规则
    decided_at      TIMESTAMP DEFAULT current_timestamp,
    note            VARCHAR
);

CREATE INDEX IF NOT EXISTS idx_vres_run ON validation_result(run_id, view_name);

-- ============================================================
-- 持仓
-- ============================================================

-- 只记录事实（股数与成本），不记录任何判断。
-- 权重、浮盈亏由查询时结合最新行情计算——避免落盘后过期。
CREATE TABLE IF NOT EXISTS position (
    code         VARCHAR PRIMARY KEY,
    shares       BIGINT NOT NULL,
    cost_price   DOUBLE NOT NULL,   -- 每股成本
    opened_at    DATE,
    updated_at   DATE NOT NULL,
    notes        VARCHAR
);
