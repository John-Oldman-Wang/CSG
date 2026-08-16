-- CSG 数据库 schema
--
-- 三条贯穿全局的设计约束：
--
-- 1. Point-in-Time：所有财务数据表带 disclosure_date（实际披露日）。
--    任何按时点回溯的查询必须以 disclosure_date 过滤，而非 report_period。
--    实测东财批量接口的「公告日期」与报告期对不上，故 disclosure_date
--    单独建表维护，来源标注在 source 字段。
--
-- 2. 复权：只存原始价格 + 复权因子，不存复权价。
--    前复权会因未来的除权事件改变历史值，落盘即错。
--
-- 3. 时点正确的股票池：维护上市/退市日期与更名历史，
--    回测时必须用「当时的」股票列表，否则产生幸存者偏差。

-- ============================================================
-- 基础信息
-- ============================================================

CREATE TABLE IF NOT EXISTS stock_basic (
    code          VARCHAR PRIMARY KEY,   -- 6 位代码，不含市场后缀
    name          VARCHAR,               -- 当前名称
    market        VARCHAR,               -- 主板 / 创业板 / 科创板 / 北交所
    exchange      VARCHAR,               -- SH / SZ / BJ
    list_date     DATE,                  -- 上市日
    delist_date   DATE,                  -- 退市日，NULL 表示仍在市
    is_active     BOOLEAN DEFAULT TRUE,
    updated_at    TIMESTAMP DEFAULT current_timestamp
);

-- 更名历史：用于判断某时点是否处于 ST 状态。
-- ST 判定不能用当前名称——一家公司现在不叫 ST，不代表 2019 年不是。
CREATE TABLE IF NOT EXISTS stock_name_history (
    code          VARCHAR NOT NULL,
    name          VARCHAR NOT NULL,
    start_date    DATE NOT NULL,
    end_date      DATE,                  -- NULL 表示至今
    is_st         BOOLEAN,               -- 由名称解析：含 ST / *ST / S*ST
    PRIMARY KEY (code, start_date)
);

-- 行业与概念归属。
-- 主键不含 start_date：当前数据源（东财）只提供当前分类，无历史变更记录，
-- start_date 恒为 NULL 而主键列不允许 NULL。字段保留，待将来接入
-- 带历史的分类源（如申万变更表）时启用。
CREATE TABLE IF NOT EXISTS industry_member (
    code          VARCHAR NOT NULL,
    taxonomy      VARCHAR NOT NULL,      -- em_industry / em_concept / sw_l1 ...
    industry_name VARCHAR NOT NULL,
    start_date    DATE,
    end_date      DATE,
    PRIMARY KEY (code, taxonomy, industry_name)
);

-- ============================================================
-- 行情
-- ============================================================

-- 原始价格（不复权）+ 复权因子。复权价在查询时计算。
CREATE TABLE IF NOT EXISTS daily_quote (
    code          VARCHAR NOT NULL,
    trade_date    DATE NOT NULL,
    open          DOUBLE,
    high          DOUBLE,
    low           DOUBLE,
    close         DOUBLE,
    volume        BIGINT,                -- 手
    amount        DOUBLE,                -- 元
    pct_chg       DOUBLE,                -- 涨跌幅 %
    turnover      DOUBLE,                -- 换手率 %
    adj_factor    DOUBLE,                -- 复权因子；后复权价 = close * adj_factor
    PRIMARY KEY (code, trade_date)
);

-- L0 全市场轻量层：只存估值与市值，用于行业分位、市场水位、超额跌幅拆分。
-- 每只每天几个数字，全市场十年约几百 MB，代价极小但保住行业与市场维度。
CREATE TABLE IF NOT EXISTS daily_basic (
    code          VARCHAR NOT NULL,
    trade_date    DATE NOT NULL,
    pe_ttm        DOUBLE,
    pb            DOUBLE,
    ps_ttm        DOUBLE,
    dv_ratio      DOUBLE,                -- 股息率 %
    total_mv      DOUBLE,                -- 总市值，元
    circ_mv       DOUBLE,                -- 流通市值，元
    PRIMARY KEY (code, trade_date)
);

CREATE TABLE IF NOT EXISTS trade_calendar (
    trade_date    DATE PRIMARY KEY,
    is_open       BOOLEAN NOT NULL
);

-- ============================================================
-- 披露日期 —— Point-in-Time 的锚点
-- ============================================================

-- 独立成表的原因：财务数据可从批量接口高效获取，但其自带的公告日期
-- 不可靠（实测与报告期错位）。真实披露日需从巨潮公告逐只解析。
-- 两者分离，可各自按最优方式采集，在查询时 JOIN。
CREATE TABLE IF NOT EXISTS disclosure_date (
    code            VARCHAR NOT NULL,
    report_period   DATE NOT NULL,       -- 报告期末，如 2024-09-30
    disclosure_date DATE NOT NULL,       -- 实际首次披露日
    report_type     VARCHAR,             -- annual / q1 / interim / q3
    source          VARCHAR NOT NULL,    -- cninfo / tushare / em
    title           VARCHAR,             -- 原始公告标题，供人工核对
    url             VARCHAR,             -- 巨潮原文链接
    PRIMARY KEY (code, report_period, source)
);

-- ============================================================
-- 财务报表
-- ============================================================
-- 均为「累计值」口径（A 股披露惯例）。单季值由查询时相减得到。
-- disclosure_date 冗余存储，避免每次查询都 JOIN；以 disclosure_date 表为准。

CREATE TABLE IF NOT EXISTS fin_income (
    code              VARCHAR NOT NULL,
    report_period     DATE NOT NULL,
    disclosure_date   DATE,
    total_revenue     DOUBLE,   -- 营业总收入
    revenue           DOUBLE,   -- 营业收入
    operating_cost    DOUBLE,   -- 营业成本
    selling_exp       DOUBLE,   -- 销售费用
    admin_exp         DOUBLE,   -- 管理费用
    rd_exp            DOUBLE,   -- 研发费用
    fin_exp           DOUBLE,   -- 财务费用
    operate_profit    DOUBLE,   -- 营业利润
    total_profit      DOUBLE,   -- 利润总额
    income_tax        DOUBLE,
    n_income          DOUBLE,   -- 净利润
    n_income_attr_p   DOUBLE,   -- 归母净利润
    basic_eps         DOUBLE,
    PRIMARY KEY (code, report_period)
);

CREATE TABLE IF NOT EXISTS fin_balance (
    code              VARCHAR NOT NULL,
    report_period     DATE NOT NULL,
    disclosure_date   DATE,
    total_assets      DOUBLE,
    total_liab        DOUBLE,
    total_equity      DOUBLE,   -- 所有者权益合计
    equity_attr_p     DOUBLE,   -- 归母权益
    money_cap         DOUBLE,   -- 货币资金
    accounts_receiv   DOUBLE,   -- 应收账款   ← AI 板块暴雷高发区
    inventories       DOUBLE,   -- 存货       ← 周期行业关键
    fix_assets        DOUBLE,   -- 固定资产
    cip               DOUBLE,   -- 在建工程   ← 扩产激进度
    goodwill          DOUBLE,   -- 商誉       ← 红旗规则
    intangible_assets DOUBLE,
    st_borr           DOUBLE,   -- 短期借款
    lt_borr           DOUBLE,   -- 长期借款
    contract_liab     DOUBLE,   -- 合同负债   ← 订单前瞻，AI 行业关键
    share_capital     DOUBLE,   -- 总股本（股）← 市值 = 收盘价 × 本字段
    treasury_shares   DOUBLE,   -- 库存股
    PRIMARY KEY (code, report_period)
);

CREATE TABLE IF NOT EXISTS fin_cashflow (
    code                  VARCHAR NOT NULL,
    report_period         DATE NOT NULL,
    disclosure_date       DATE,
    n_cashflow_act        DOUBLE,   -- 经营活动现金流净额 ← 最重要的红旗指标
    n_cashflow_inv_act    DOUBLE,   -- 投资活动
    n_cashflow_fin_act    DOUBLE,   -- 筹资活动
    c_pay_acq_const       DOUBLE,   -- 购建固定资产等支付的现金（资本开支）
    depreciation          DOUBLE,
    free_cashflow         DOUBLE,   -- 经营现金流 - 资本开支
    PRIMARY KEY (code, report_period)
);

-- ============================================================
-- 研报
-- ============================================================

-- 用途限定（见 docs/AI_ASSIST.md 第七节）：把研报当**数据源**，不当观点源。
-- 评级本身信息量极低（券商买入评级常年占 95%+），入库是为了**验证它到底
-- 有没有预测力**——测出「无效」同样是有价值的结论，可据此永久关掉这个输入。
CREATE TABLE IF NOT EXISTS research_report (
    code           VARCHAR NOT NULL,
    publish_date   DATE NOT NULL,
    institution    VARCHAR NOT NULL,
    title          VARCHAR NOT NULL,
    rating         VARCHAR,            -- 东财评级：买入/增持/中性/减持
    industry       VARCHAR,
    pdf_url        VARCHAR,
    researcher     VARCHAR,            -- 研究员姓名（同花顺有，东财无）
    -- 数据源：'em' 东方财富 / 'ths' 同花顺。
    --
    -- **不做跨源去重。** 同一份研报两边都收到时，标题措辞常有差异
    -- （东财含机构前缀、同花顺不含；副标题取舍也不同），强行合并
    -- 只会丢信息。保留双份的收益是可交叉校验：同一 (code, 日期, 机构)
    -- 若两源评级不一致，说明至少一边解析错了——这是免费的质量检查。
    source         VARCHAR NOT NULL DEFAULT 'em',
    snapshot_date  DATE NOT NULL,      -- 本行的采集日期
    PRIMARY KEY (code, publish_date, institution, title, source)
);

-- 盈利预测（长表）。
--
-- 用长表而非宽表：源接口的预测年份列是**动态**的（当前为 2026/2027/2028），
-- 宽表结构会随时间失效。
--
-- ⚠️ 历史局限：接口只返回「对当前及未来年份的预测」，2018-2023 年发布的
--    研报其预测字段全为空（实测非空率 0%）。因此**历史盈利预测准确度
--    无法回溯验证**——这是数据壁垒，非实现问题。
--
-- snapshot_date 进入主键的原因：同一份研报在不同时间采集可能取到不同的
-- 预测值（数据方会回填修订）。保留每次快照，才能积累出「一致预期随时间
-- 变动」的序列——而预期差正是研报真正有价值的部分。今天开始存，
-- 一年后就拥有一段买不到的历史。
CREATE TABLE IF NOT EXISTS research_forecast (
    code           VARCHAR NOT NULL,
    publish_date   DATE NOT NULL,
    institution    VARCHAR NOT NULL,
    forecast_year  INTEGER NOT NULL,
    eps            DOUBLE,
    pe             DOUBLE,
    researcher     VARCHAR,
    source         VARCHAR NOT NULL DEFAULT 'em',
    snapshot_date  DATE NOT NULL,
    PRIMARY KEY (code, publish_date, institution, forecast_year, snapshot_date, source)
);

-- ============================================================
-- 管线状态 —— 幂等自愈的基础
-- ============================================================

-- 笔记本会休眠、断网、关机，调度不可靠。
-- 所有任务按「查最后成功水位 → 补齐缺口」执行，而非「跑今天的」。
CREATE TABLE IF NOT EXISTS sync_watermark (
    dataset           VARCHAR NOT NULL,  -- daily_quote / fin_income / ...
    scope             VARCHAR NOT NULL,  -- 股票代码，或 '__ALL__' 表示全局
    last_success_date DATE,              -- 已成功覆盖到的日期
    last_run_at       TIMESTAMP,
    status            VARCHAR,           -- ok / partial / failed
    error             VARCHAR,
    PRIMARY KEY (dataset, scope)
);

-- 跨源分歧记录：关键字段多源比对不一致时告警，而非静默采用某一方
CREATE TABLE IF NOT EXISTS data_conflict (
    detected_at   TIMESTAMP DEFAULT current_timestamp,
    dataset       VARCHAR,
    code          VARCHAR,
    ref_date      DATE,
    field         VARCHAR,
    source_a      VARCHAR,
    value_a       DOUBLE,
    source_b      VARCHAR,
    value_b       DOUBLE,
    rel_diff      DOUBLE
);

-- ============================================================
-- 索引
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_quote_date    ON daily_quote(trade_date);
CREATE INDEX IF NOT EXISTS idx_basic_date    ON daily_basic(trade_date);
CREATE INDEX IF NOT EXISTS idx_disc_date     ON disclosure_date(disclosure_date);
CREATE INDEX IF NOT EXISTS idx_income_disc   ON fin_income(disclosure_date);
CREATE INDEX IF NOT EXISTS idx_balance_disc  ON fin_balance(disclosure_date);
CREATE INDEX IF NOT EXISTS idx_cashflow_disc ON fin_cashflow(disclosure_date);
CREATE INDEX IF NOT EXISTS idx_industry_name ON industry_member(taxonomy, industry_name);
CREATE INDEX IF NOT EXISTS idx_report_date   ON research_report(publish_date);
CREATE INDEX IF NOT EXISTS idx_report_code   ON research_report(code, publish_date);


-- 指数行情。
--
-- 独立于 daily_quote：指数无复权概念（本身即点位序列），
-- 混入个股表会让「所有查询都要记得排除指数」，迟早漏掉一处。
CREATE TABLE IF NOT EXISTS index_quote (
    code        VARCHAR NOT NULL,
    name        VARCHAR,
    trade_date  DATE    NOT NULL,
    open        DOUBLE,
    high        DOUBLE,
    low         DOUBLE,
    close       DOUBLE,
    volume      DOUBLE,
    amount      DOUBLE,
    pct_chg     DOUBLE,
    PRIMARY KEY (code, trade_date)
);
