import type {
  BuyRule,
  Candle,
  ConclusionInput,
  CsgEvent,
  ExitSignal,
  FinancialPeriod,
  HealthInfo,
  Institution,
  InstitutionCurvesResp,
  InstitutionOption,
  InstitutionPnlResp,
  InstitutionStats,
  InstitutionTradesResp,
  PoolTiersResp,
  PortfolioResp,
  ReportAnalysis,
  ReportFilters,
  ReportSearchResult,
  ResearchReport,
  ReviewTask,
  StockBasic,
  StockOverview,
  ValidationRun,
  WatchlistEntry,
  WinRatesResp,
} from "@/types";

const BASE = "/api";

/** 后端在采集任务运行期间会返回 503（DuckDB 单写锁），
 *  这不是错误而是已知状态，需要与真正的故障区分开。 */
export class DataLockedError extends Error {}

async function get<T>(path: string, params?: Record<string, string | number>): Promise<T> {
  const url = new URL(`${BASE}${path}`, window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      url.searchParams.set(k, String(v));
    }
  }
  const res = await fetch(url.toString());
  if (res.status === 503) {
    const body = await res.json().catch(() => ({ detail: "数据更新中" }));
    throw new DataLockedError(body.detail);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(body.detail ?? `请求失败 ${res.status}`);
  }
  return res.json() as Promise<T>;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 503) throw new DataLockedError("数据库被占用，稍后重试");
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? `提交失败 ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => get<HealthInfo>("/health"),

  pool: () =>
    get<{
      total: number;
      breakdown: { theme: string; industry_name: string; count: number }[];
      stocks: StockBasic[];
    }>("/pool"),

  stock: (code: string) =>
    get<{ basic: StockBasic; watchlist: WatchlistEntry | null }>(`/stocks/${code}`),

  quotes: (code: string, start = "2016-01-01", adjust: "hfq" | "none" = "hfq") =>
    get<Candle[]>(`/stocks/${code}/quotes`, { start, adjust }),

  financials: (code: string, asOf?: string) =>
    get<{ as_of: string; periods: FinancialPeriod[] }>(
      `/stocks/${code}/financials`,
      asOf ? { as_of: asOf } : undefined,
    ),

  reports: (code: string, limit = 100) =>
    get<ResearchReport[]>(`/stocks/${code}/reports`, { limit }),

  events: (limit = 100) => get<CsgEvent[]>("/events", { limit }),

  tasks: (status = "pending") => get<ReviewTask[]>("/tasks", { status }),

  task: (id: string) => get<ReviewTask>(`/tasks/${id}`),

  conclude: (id: string, body: ConclusionInput) =>
    post<{ ok: boolean; warning: string | null }>(`/tasks/${id}/conclude`, body),

  validations: () => get<ValidationRun[]>("/validations"),

  validation: (runId: string) =>
    get<{
      run_id: string;
      validation_type: string;
      run_at: string;
      params: Record<string, unknown>;
      data_snapshot: Record<string, unknown>;
      results: Record<string, Record<string, unknown>[]>;
    }>(`/validations/${runId}`),

  searchReports: (filters: ReportFilters) => {
    const params: Record<string, string | number> = {};
    for (const [k, v] of Object.entries(filters)) {
      if (v === undefined || v === null || v === "") continue;
      params[k] = v as string | number;
    }
    return get<ReportSearchResult>("/reports", params);
  },

  reportAnalysis: (reportId: string) => get<ReportAnalysis>(`/reports/${reportId}/analysis`),

  stockOverview: (code: string) => get<StockOverview>(`/stocks/${code}/overview`),

  institutions: (minReports = 5, keyword?: string) =>
    get<Institution[]>(
      "/institutions",
      keyword ? { min_reports: minReports, keyword } : { min_reports: minReports },
    ),

  reportIndustries: () => get<{ name: string; report_count: number }[]>("/report-industries"),

  institutionStats: (params: {
    horizon?: number;
    group_by?: "year" | "half" | "quarter" | "all";
    min_samples?: number;
    institution?: string;
  }) => {
    const q: Record<string, string | number> = {};
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== "") q[k] = v as string | number;
    }
    return get<InstitutionStats>("/institution-stats", q);
  },

  institutionWinRates: (horizon = 60, minSamples = 10) =>
    get<WinRatesResp>("/institution-winrates", {
      horizon,
      min_samples: minSamples,
    }),

  institutionPnl: (horizon = 20, capital = 10000, minSamples = 50) =>
    get<InstitutionPnlResp>("/institution-pnl", {
      horizon,
      capital,
      min_samples: minSamples,
    }),

  institutionCurves: (
    horizon = 20,
    buy: BuyRule = "bullish",
    exitSignal: ExitSignal = "none",
    capital = 10000,
    minTrades = 100,
  ) =>
    get<InstitutionCurvesResp>("/institution-curves", {
      horizon,
      capital,
      buy,
      exit_signal: exitSignal,
      min_trades: minTrades,
    }),

  institutionOptions: (minReports = 30) =>
    get<InstitutionOption[]>("/institution-options", { min_reports: minReports }),

  institutionTrades: (
    institution: string,
    horizon = 20,
    buy: BuyRule = "bullish",
    exitSignal: ExitSignal = "none",
    capital = 10000,
  ) =>
    get<InstitutionTradesResp>("/institution-trades", {
      institution,
      horizon,
      capital,
      buy,
      exit_signal: exitSignal,
    }),

  poolTiers: () => get<PoolTiersResp>("/pool-tiers"),

  portfolio: () => get<PortfolioResp>("/portfolio"),

  watchlist: () => get<(WatchlistEntry & { name: string | null })[]>("/watchlist"),
};
