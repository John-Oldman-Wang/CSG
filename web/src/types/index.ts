/** 与后端 csg.api 对应的类型定义。
 *
 * ⚠️ 财务字段命名必须与后端严格一致，尤其是：
 *   report_period      报告期（财报所属期间）
 *   disclosure_date    披露日 ← Point-in-Time 的判定依据
 * 二者混用会静默产生未来函数，类型系统是最后一道防线。
 */

export interface StockBasic {
  code: string;
  name: string;
  market: string;
  exchange: string;
  list_date: string | null;
  delist_date: string | null;
  is_active: boolean;
  industry_name: string | null;
}

export interface WatchlistEntry {
  code: string;
  added_at: string;
  tier: "watch" | "holding";
  thesis: string | null;
  core_assumptions: string | null;
  /** 证伪条件：方法论的枢纽，L6 监控的直接输入 */
  falsification: string | null;
  target_price: number | null;
}

export interface Candle {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  amount: number;
  pct_chg: number;
}

export interface FinancialPeriod {
  code: string;
  report_period: string;
  /** 该期财报的实际披露日；PIT 查询以此过滤，而非 report_period */
  disclosure_date: string | null;
  roe_ttm: number | null;
  net_margin_ttm: number | null;
  gross_margin_ttm: number | null;
  /** 经营现金流 / 净利润 —— 单项最重要的红旗指标 */
  cfo_to_ni: number | null;
  debt_ratio: number | null;
  goodwill_to_equity: number | null;
  capex_to_revenue: number | null;
  contract_liab_to_revenue: number | null;
  revenue_yoy: number | null;
  profit_yoy: number | null;
  flag_score: number;
  flag_count: number;
  flag_names: string;
  [key: string]: unknown;
}

export interface ResearchReport {
  publish_date: string;
  institution: string;
  title: string;
  rating: string | null;
  /** 同机构上一次评级；变化才有信息量，绝对评级几乎无区分度 */
  prev_rating: string | null;
  pdf_url: string | null;
}

export type Severity = "P0" | "P1" | "P2";

export interface TaskContext {
  facts: [string, string][];
  questions: string[];
}

export interface ReviewTask {
  task_id: string;
  event_id: string;
  code: string;
  name: string | null;
  task_type: string;
  severity: Severity;
  title: string;
  context: TaskContext | null;
  status: "pending" | "notified" | "in_review" | "concluded";
  created_at: string;
  due_at: string;
  overdue: boolean;
  watchlist?: WatchlistEntry | null;
}

/** 复核结论。三选一，判断权在人。 */
export type Verdict = "sentiment" | "fundamental" | "insufficient";

export interface ConclusionInput {
  verdict: Verdict;
  /** 强制必答：以今天的价格与信息，我会重新买入吗？切断沉没成本 */
  would_rebuy: boolean;
  reasoning: string;
  falsified_items?: string | null;
  /** verdict=insufficient 时必填，不允许无限期挂起 */
  next_review_date?: string | null;
  action_taken?: "none" | "add" | "reduce" | "exit";
}

export interface CsgEvent {
  event_id: string;
  event_type: string;
  code: string;
  name: string | null;
  ref_date: string;
  severity: Severity;
  title: string;
  payload: string | null;
  detected_at: string;
}

export interface HealthInfo {
  counts: { name: string; rows: number }[];
  watermarks: {
    dataset: string;
    scopes: number;
    failed: number;
    newest: string | null;
  }[];
}

export interface ValidationRun {
  run_id: string;
  validation_type: string;
  run_at: string;
  note: string | null;
  研报数: string | null;
  行情行数: string | null;
  结果行数: number;
}

/** 评级调整方向。down 最具信息量——逆着分析师的激励机制。 */
export type RatingChange = "up" | "down" | "unchanged" | "first";

export interface ReportRow {
  report_id: string;
  title: string;
  publish_date: string;
  institution: string;
  code: string;
  stock_name: string | null;
  industry: string | null;
  rating: string | null;
  prev_rating: string | null;
  rating_change: RatingChange;
  has_forecast: boolean;
  pdf_url: string | null;
  /** 发布后 N 日**超额**收益（个股收益 − 同月全样本中位数）。
   *  窗口未走完时为 null —— 不是 0，也不用当前价凑数。 */
  ret_20: number | null;
  ret_50: number | null;
  ret_100: number | null;
  /** 自入场日起已走完的交易日数。用于把「窗口未满」与「数据缺失」区分开。
   *  null 表示发布后还没有交易日，尚未入场。 */
  elapsed_days: number | null;
}

export interface ReportSearchResult {
  total: number;
  page: number;
  page_size: number;
  horizons: number[];
  items: ReportRow[];
}

export interface ReportFilters {
  start?: string;
  end?: string;
  title?: string;
  institution?: string;
  code?: string;
  stock?: string;
  industry?: string;
  rating?: string;
  rating_change?: RatingChange;
  page?: number;
  page_size?: number;
}

export interface Institution {
  name: string;
  report_count: number;
  stock_count: number;
  first_date: string;
  last_date: string;
}

export interface StockOverview {
  basic: StockBasic;
  quote: {
    trade_date: string;
    close: number;
    pct_chg: number;
    turnover: number;
    volume: number;
    amount: number;
  } | null;
  /** 估值来自 daily_basic（baostock K线接口附带）。
   *  该表为空时整体为 null —— 缺失与零是两回事，前端须显示「—」。 */
  valuation: {
    trade_date: string;
    pe_ttm: number | null;
    pb: number | null;
    ps_ttm: number | null;
    total_mv: number | null;
    circ_mv: number | null;
  } | null;
  band: { high_52w: number | null; low_52w: number | null } | null;
}

export interface Forecast {
  forecast_year: number;
  eps: number | null;
  pe: number | null;
  snapshot_date: string;
  /** ⚠️ 推算值 = 预测EPS × 发布日PE，**不是研报目标价**。
   *  接口不提供目标价，其「预测PE」实为发布日股价÷预测EPS。 */
  implied_price: number | null;
  basis: string;
}

export interface ReportPerformance {
  entry_price: number | null;
  ret_20: number | null;
  ret_60: number | null;
  ret_120: number | null;
  ret_250: number | null;
  max_gain: number | null;
  max_loss: number | null;
}

export interface ReportAnalysis {
  report: ReportRow & { industry: string | null; prev_rating: string | null };
  forecasts: Forecast[];
  quotes: Candle[];
  performance: ReportPerformance | null;
  valuation_at_publish: {
    trade_date: string;
    pe_ttm: number | null;
    pb: number | null;
    total_mv: number | null;
  } | null;
  price_at_publish: number | null;
  /** 研报发布时点**已披露**的财务，非最新——
   *  用最新数据评价两年前的研报，是拿他不可能知道的信息苛责他。 */
  financials_pit: FinancialPeriod[];
  pit_note: string;
}

export interface InstitutionStatRow {
  期间: string;
  机构: string;
  样本数: number;
  超额中位: number | null;
  胜率: number | null;
  绝对收益中位: number | null;
  最差: number | null;
  最好: number | null;
}

export interface InstitutionStats {
  horizon: number;
  group_by: string;
  coverage: { 研报总数: number; 最新研报: string; 行情末日: string } | null;
  rows: InstitutionStatRow[];
  caveat: string;
}

export interface PoolTier {
  tier: string;
  name: string;
  count: number;
  desc: string;
}

export interface WatchlistRow {
  code: string;
  name: string | null;
  industry: string | null;
  tier: string;
  added_at: string;
  thesis: string | null;
  core_assumptions: string | null;
  falsification: string | null;
  target_price: number | null;
  in_position: boolean;
  /** L1→L2 晋升的实质门槛：写不出证伪条件说明还没想清楚 */
  falsification_count: number;
}

export interface PoolTiersResp {
  tiers: PoolTier[];
  l1_breakdown: { theme: string; industry_name: string; count: number }[];
  watchlist: WatchlistRow[];
  holdings: Record<string, unknown>[];
  promotion_gate: string;
  coverage: {
    l1_researched: number;
    l2_held: number;
    /** 已持仓但未写假设——跳级买入的痕迹 */
    held_not_watched: string[];
  };
}

export interface PortfolioHolding {
  code: string;
  name: string | null;
  industry: string | null;
  shares: number;
  cost_price: number;
  price: number;
  market_value: number;
  cost_value: number;
  weight: number;
  pnl: number;
  pnl_pct: number;
  theme: string;
}

export interface PortfolioResp {
  holdings: PortfolioHolding[];
  violations: {
    rule: string;
    severity: string;
    target: string;
    current: number;
    limit: number;
    message: string;
  }[];
  summary: {
    total_market_value: number;
    total_cost: number;
    pnl: number;
    pnl_pct: number;
    count: number;
    count_limit: string;
    industry_breakdown: { industry: string; weight: number }[];
    theme_breakdown: { theme: string; weight: number }[];
  } | null;
}

export interface WinRateRow {
  机构: string;
  n1: number;
  n2: number;
  n3: number; // 近1/2/3年样本数
  w1: number | null;
  w2: number | null;
  w3: number | null; // 胜率
  e1: number | null;
  e2: number | null;
  e3: number | null; // 超额收益中位
}

export interface WinRatesResp {
  horizon: number;
  as_of: string;
  total: number;
  rows: WinRateRow[];
  caveat: string;
}

/** 机构盈亏：每份研报投入固定金额、持有 N 日的累计**超额**盈亏。 */
export interface InstitutionPnlRow {
  机构: string;
  发现份数: number;
  发现累计: number;
  发现胜率: number | null;
  验证份数: number;
  验证累计: number;
  验证胜率: number | null;
  发现排名: number;
  验证排名: number;
  名次变化: number;
}

export interface InstitutionPnlResp {
  horizon: number;
  capital: number;
  min_samples: number;
  rows: InstitutionPnlRow[];
  stability: {
    机构数: number;
    秩相关: number;
    "前1/3留存": number;
    随机基准: number;
    两期皆盈: number;
  };
}

export interface TradeRow {
  发布日: string;
  代码: string;
  股票: string | null;
  标题: string;
  评级: string | null;
  买入日: string;
  买入价: number;
  卖出日: string;
  卖出价: number;
  收益率: number;
  盈亏: number;
  /** 持有期内发生除权除息：显示价格手算的涨幅与「收益率」不符，差额为分红送转 */
  除权: boolean;
}

export interface InstitutionTradesResp {
  institution: string;
  horizon: number;
  capital: number;
  summary: {
    笔数: number;
    总盈亏: number;
    /** 峰值同时持仓 × 单笔金额 —— 真正要准备的本金，非「单笔 × 笔数」 */
    占用资金: number;
    峰值并发: number;
    平均并发: number;
    周转次数: number | null;
    年数: number;
    累计收益率: number | null;
    年化收益率: number | null;
    最大回撤: number;
    涨停顺延笔数: number;
    胜率: number;
    盈利笔数: number;
    亏损笔数: number;
    平均收益率: number;
    中位收益率: number;
    最好: number;
    最差: number;
    盈亏比: number | null;
    含除权笔数: number;
  } | null;
  curve?: { 发布日: string; 盈亏: number; 累计: number }[];
  trades: TradeRow[];
}

/** 逐笔复盘页的机构下拉项（与研报页的 InstitutionListRow 不同）。 */
export interface InstitutionOption {
  机构: string;
  研报数: number;
  最早: string;
  最晚: string;
}

export interface CurvePoint {
  月: string;
  累计盈亏: number;
  累计收益率: number;
  累计笔数: number;
}

export interface CurveInstitution {
  机构: string;
  笔数: number;
  总盈亏: number;
  /** **峰值同时持仓 × 单笔金额** —— 你真正要准备的本金。
   *  不是「单笔 × 笔数」：持有期结束卖出后，那笔钱回来给下一笔用。 */
  占用资金: number;
  峰值并发: number;
  平均并发: number;
  周转次数: number | null;
  年数: number;
  累计收益率: number;
  年化收益率: number | null;
  胜率: number;
  平均收益率: number;
  中位收益率: number;
  盈亏比: number | null;
  涨停顺延笔数: number;
  最大回撤: number;
  曲线: CurvePoint[];
}

export interface InstitutionCurvesResp {
  horizon: number;
  capital: number;
  min_trades: number;
  entry_rule: string;
  benchmark: CurveInstitution;
  beat_benchmark: number;
  institutions: CurveInstitution[];
}
