import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  AllCommunityModule,
  type ColDef,
  ModuleRegistry,
  themeQuartz,
  type ValueFormatterParams,
} from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, DataLockedError } from "@/api/client";
import {
  Badge,
  Button,
  Card,
  CardTitle,
  DataLocked,
  ReturnCell,
  WinRateBadge,
} from "@/components/ui/primitives";
import { useTheme } from "@/lib/theme";
import type { RatingChange, ReportFilters, ReportRow } from "@/types";

ModuleRegistry.registerModules([AllCommunityModule]);

/** ag-grid 配色由 JS 参数设定，不随 CSS 变量切换，故按主题各备一套，
 *  避免出现与其余界面割裂的白底（或黑底）表格。 */
const GRID_THEMES = {
  dark: themeQuartz.withParams({
    backgroundColor: "#1a1d23",
    foregroundColor: "#e8e9ec",
    headerBackgroundColor: "#22262e",
    headerTextColor: "#9aa0aa",
    borderColor: "#333842",
    rowHoverColor: "#22262e",
    oddRowBackgroundColor: "transparent",
    fontFamily: "inherit",
    fontSize: 13,
    headerFontSize: 12,
  }),
  light: themeQuartz.withParams({
    backgroundColor: "#fbfbfc",
    foregroundColor: "#2b2f3a",
    headerBackgroundColor: "#f2f3f6",
    headerTextColor: "#5a6072",
    borderColor: "#dcdee5",
    rowHoverColor: "#f2f3f6",
    oddRowBackgroundColor: "transparent",
    fontFamily: "inherit",
    fontSize: 13,
    headerFontSize: 12,
  }),
} as const;

/** 评级调整方向。
 *  down 单独高亮：绝对评级 94% 是买入、几乎无区分度，
 *  真正携带信息的是「谁改了主意」，尤其是逆着激励机制的下调。 */
// 配色语义分离：
//
//   红/绿  —— **数值语义**（A 股红涨绿跌）：胜率、收益、涨跌幅
//   橙     —— **警示语义**：需要注意的事件
//   蓝/灰  —— 中性信息
//
// 「评级下调」此前用 P0（红），与胜率徽章的红（表示"好"）语义冲突：
// 同一张表里两种红分别代表好与坏，必然误读。故改用 warn（橙）。
const CHANGE_META: Record<RatingChange, { label: string; tone: "warn" | "P2" | "default" }> = {
  down: { label: "下调", tone: "warn" }, // 警示，非"利空数值"
  up: { label: "上调", tone: "default" }, // 已验证无信息量，不着色
  unchanged: { label: "维持", tone: "default" },
  first: { label: "首次", tone: "P2" }, // 中性信息
};

const PAGE_SIZE = 50;

export default function Reports() {
  const { theme } = useTheme();
  // URL 是筛选状态的唯一真相源：链接可分享、刷新不丢、浏览器前进后退可用。
  // form 只是用户正在输入的临时态，点「查询」后才写回 URL。
  const [searchParams, setSearchParams] = useSearchParams();
  const applied = useMemo(() => parseFilters(searchParams), [searchParams]);
  const page = Math.max(1, Number(searchParams.get("page") ?? 1));
  const [form, setForm] = useState<ReportFilters>(() => parseFilters(searchParams));

  // 浏览器前进/后退时同步表单，否则输入框会与 URL 脱节
  useEffect(() => {
    setForm(parseFilters(searchParams));
  }, [searchParams]);

  function commit(next: ReportFilters, nextPage: number) {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(next)) {
      if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
    }
    if (nextPage > 1) params.set("page", String(nextPage));
    setSearchParams(params);
  }

  const institutions = useQuery({
    queryKey: ["institutions"],
    queryFn: () => api.institutions(10),
  });
  const industries = useQuery({
    queryKey: ["reportIndustries"],
    queryFn: api.reportIndustries,
  });

  // 机构历史胜率：一次取回做成 Map，供表格逐行查找，
  // 避免每行单独请求
  const winrates = useQuery({
    queryKey: ["institutionWinRates"],
    queryFn: () => api.institutionWinRates(60, 10),
    staleTime: 5 * 60_000,
  });
  const wrMap = useMemo(() => {
    const m = new Map<string, { w: number | null; n: number }>();
    for (const r of winrates.data?.rows ?? []) {
      m.set(r.机构, { w: r.w3, n: r.n3 });
    }
    return m;
  }, [winrates.data]);

  const query = useQuery({
    queryKey: ["reports", applied, page],
    queryFn: () => api.searchReports({ ...applied, page, page_size: PAGE_SIZE }),
    // 翻页时保留上一页数据，避免表格闪烁成空白
    placeholderData: keepPreviousData,
  });

  const columns = useMemo<ColDef<ReportRow>[]>(
    () => [
      {
        field: "publish_date",
        headerName: "发布日期",
        width: 125,
        valueFormatter: (p: ValueFormatterParams) => String(p.value ?? "").slice(0, 10),
        cellClass: "num",
      },
      {
        field: "code",
        headerName: "代码",
        width: 90,
        cellClass: "num",
        cellRenderer: (p: { value: string }) => (
          <Link to={`/stocks/${p.value}`} className="underline underline-offset-2">
            {p.value}
          </Link>
        ),
      },
      { field: "stock_name", headerName: "股票", width: 110 },
      { field: "industry", headerName: "行业", width: 100 },
      {
        field: "institution",
        headerName: "机构",
        width: 190,
        cellRenderer: (p: { value: string }) => {
          const wr = wrMap.get(p.value);
          return (
            <span className="flex items-center gap-1.5">
              <span className="truncate">{p.value}</span>
              {wr && <WinRateBadge rate={wr.w} samples={wr.n} minSamples={10} />}
            </span>
          );
        },
      },
      {
        field: "title",
        headerName: "报告名称",
        flex: 1,
        minWidth: 240,
        tooltipField: "title",
        cellRenderer: (p: { data?: ReportRow }) =>
          p.data ? (
            <Link
              to={`/reports/${p.data.report_id}`}
              className="underline decoration-[var(--color-border)] underline-offset-2 hover:decoration-[var(--color-fg)]"
            >
              {p.data.title}
            </Link>
          ) : null,
      },
      {
        field: "rating_change",
        headerName: "评级变化",
        width: 130,
        cellRenderer: (p: { data?: ReportRow }) => {
          const d = p.data;
          if (!d) return null;
          const meta = CHANGE_META[d.rating_change];
          return (
            <span className="flex items-center gap-1.5">
              <Badge tone={meta.tone}>{meta.label}</Badge>
              {d.prev_rating && d.rating_change !== "first" && (
                <span className="text-[var(--color-muted)] text-xs">
                  {d.prev_rating}→{d.rating}
                </span>
              )}
            </span>
          );
        },
      },
      ...([20, 50, 100] as const).map(
        (h): ColDef<ReportRow> => ({
          field: `ret_${h}` as keyof ReportRow,
          headerName: `${h}日超额`,
          width: 108,
          type: "numericColumn",
          cellRenderer: (p: { data?: ReportRow }) =>
            p.data ? (
              <ReturnCell
                value={p.data[`ret_${h}`]}
                horizon={h}
                elapsed={p.data.elapsed_days}
              />
            ) : null,
        }),
      ),
      {
        field: "has_forecast",
        headerName: "预测",
        width: 70,
        cellRenderer: (p: { value: boolean }) => (p.value ? <Badge tone="P2">有</Badge> : null),
      },
      {
        field: "pdf_url",
        headerName: "原文",
        width: 70,
        sortable: false,
        cellRenderer: (p: { value: string | null }) =>
          p.value ? (
            <a href={p.value} target="_blank" rel="noreferrer" className="underline">
              PDF
            </a>
          ) : null,
      },
    ],
    [wrMap],
  );

  function set<K extends keyof ReportFilters>(key: K, value: ReportFilters[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  function search() {
    commit(form, 1);
  }

  function reset() {
    setForm({});
    setSearchParams(new URLSearchParams());
  }

  if (query.error instanceof DataLockedError) {
    return <DataLocked message={query.error.message} />;
  }

  const total = query.data?.total ?? 0;
  const maxPage = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="space-y-4">
      <Card>
        <CardTitle
          extra={
            <span className="text-[var(--color-muted)] text-xs">
              绝对评级无区分度（买入占 94%），筛「下调」更有信息量
            </span>
          }
        >
          研报检索
        </CardTitle>

        <div className="grid gap-3 md:grid-cols-4">
          <Field label="起始日期">
            <input
              type="date"
              value={form.start ?? ""}
              onChange={(e) => set("start", e.target.value)}
              className={inputCls}
            />
          </Field>
          <Field label="截止日期">
            <input
              type="date"
              value={form.end ?? ""}
              onChange={(e) => set("end", e.target.value)}
              className={inputCls}
            />
          </Field>
          <Field label="报告标题（模糊）">
            <input
              value={form.title ?? ""}
              onChange={(e) => set("title", e.target.value)}
              placeholder="如：业绩、点评、深度"
              className={inputCls}
            />
          </Field>
          <Field label="股票（代码或名称）">
            <input
              value={form.stock ?? ""}
              onChange={(e) => set("stock", e.target.value)}
              placeholder="如：300750 或 宁德"
              className={inputCls}
            />
          </Field>

          <Field label="机构">
            <input
              list="institution-list"
              value={form.institution ?? ""}
              onChange={(e) => set("institution", e.target.value)}
              placeholder="输入或选择"
              className={inputCls}
            />
            <datalist id="institution-list">
              {institutions.data?.map((i) => (
                <option key={i.name} value={i.name}>
                  {i.report_count} 篇 · {i.stock_count} 只
                </option>
              ))}
            </datalist>
          </Field>

          <Field label="行业">
            <select
              value={form.industry ?? ""}
              onChange={(e) => set("industry", e.target.value)}
              className={inputCls}
            >
              <option value="">全部</option>
              {industries.data?.map((i) => (
                <option key={i.name} value={i.name}>
                  {i.name}（{i.report_count}）
                </option>
              ))}
            </select>
          </Field>

          <Field label="评级变化">
            <select
              value={form.rating_change ?? ""}
              onChange={(e) =>
                set("rating_change", (e.target.value || undefined) as RatingChange)
              }
              className={inputCls}
            >
              <option value="">全部</option>
              <option value="down">下调 ← 最具信息量</option>
              <option value="up">上调</option>
              <option value="unchanged">维持</option>
              <option value="first">首次覆盖</option>
            </select>
          </Field>

          <Field label="评级">
            <select
              value={form.rating ?? ""}
              onChange={(e) => set("rating", e.target.value)}
              className={inputCls}
            >
              <option value="">全部</option>
              {["买入", "增持", "持有", "中性", "减持", "卖出"].map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </Field>
        </div>

        <p className="mt-3 border-[var(--color-border)] border-t pt-3 text-[var(--color-muted)] text-xs leading-relaxed">
          机构名后的徽章为<b>该机构近三年胜率</b>（60 交易日窗口，超额收益口径，
          括号内为样本量，不足 10 条置灰）。
          <span className="text-[var(--color-p1)]">
            ⚠️ 不可据此挑选机构——实测机构排名跨期秩相关 −0.286，历史排名不可外推。
          </span>
          它的用途是提供背景，例如「这家机构历史胜率仅四成」有助于你对这份研报 保持应有的怀疑。
        </p>

        <div className="mt-3 flex items-center gap-2">
          <Button onClick={search}>查询</Button>
          <Button variant="ghost" onClick={reset}>
            重置
          </Button>
          {query.isFetching && (
            <span className="text-[var(--color-muted)] text-xs">加载中…</span>
          )}
          <span className="ml-auto text-[var(--color-muted)] text-sm">
            共 <span className="num">{total.toLocaleString()}</span> 条
          </span>
        </div>
      </Card>

      <Card className="p-0">
        <div style={{ height: 560 }}>
          <AgGridReact<ReportRow>
            theme={GRID_THEMES[theme]}
            rowData={query.data?.items ?? []}
            columnDefs={columns}
            defaultColDef={{ sortable: true, resizable: true, suppressMovable: false }}
            // 服务端分页：1.8 万条不可能一次性下发，
            // 由后端 LIMIT/OFFSET 控制，此处只渲染当前页
            suppressPaginationPanel
            tooltipShowDelay={300}
            overlayNoRowsTemplate="<span style='color:#9aa0aa'>无匹配研报</span>"
          />
        </div>
      </Card>

      <div className="flex items-center justify-center gap-3">
        <Button
          variant="ghost"
          onClick={() => commit(applied, Math.max(1, page - 1))}
          disabled={page <= 1}
        >
          上一页
        </Button>
        <span className="num text-sm">
          {page} / {maxPage}
        </span>
        <Button
          variant="ghost"
          onClick={() => commit(applied, Math.min(maxPage, page + 1))}
          disabled={page >= maxPage}
        >
          下一页
        </Button>
        <Badge>每页 {PAGE_SIZE}</Badge>
      </div>
    </div>
  );
}

/** URL query → 筛选条件。只认白名单字段，避免把任意参数透传给后端。 */
const FILTER_KEYS = [
  "start",
  "end",
  "title",
  "institution",
  "code",
  "stock",
  "industry",
  "rating",
  "rating_change",
] as const;

function parseFilters(params: URLSearchParams): ReportFilters {
  const out: ReportFilters = {};
  for (const k of FILTER_KEYS) {
    const v = params.get(k);
    if (v) (out as Record<string, string>)[k] = v;
  }
  return out;
}

const inputCls =
  "w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 text-sm";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[var(--color-muted)] text-xs">{label}</span>
      {children}
    </label>
  );
}
