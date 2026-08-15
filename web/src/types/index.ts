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
