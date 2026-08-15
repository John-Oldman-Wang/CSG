import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  AllCommunityModule,
  type ColDef,
  ModuleRegistry,
  themeQuartz,
  type ValueFormatterParams,
} from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, DataLockedError } from "@/api/client";
import { Badge, Button, Card, CardTitle, DataLocked } from "@/components/ui/primitives";
import type { RatingChange, ReportFilters, ReportRow } from "@/types";

ModuleRegistry.registerModules([AllCommunityModule]);

/** ag-grid 主题对齐项目暗色配色，避免出现与其余界面割裂的白底表格。 */
const gridTheme = themeQuartz.withParams({
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
});

/** 评级调整方向。
 *  down 单独高亮：绝对评级 94% 是买入、几乎无区分度，
 *  真正携带信息的是「谁改了主意」，尤其是逆着激励机制的下调。 */
const CHANGE_META: Record<RatingChange, { label: string; cls: string }> = {
  down: { label: "下调", cls: "text-[var(--color-p0)] font-medium" },
  up: { label: "上调", cls: "text-[var(--color-muted)]" },
  unchanged: { label: "维持", cls: "text-[var(--color-muted)] opacity-60" },
  first: { label: "首次", cls: "text-[var(--color-p2)]" },
};

const PAGE_SIZE = 50;

export default function Reports() {
  const [form, setForm] = useState<ReportFilters>({});
  const [applied, setApplied] = useState<ReportFilters>({});
  const [page, setPage] = useState(1);

  const institutions = useQuery({
    queryKey: ["institutions"],
    queryFn: () => api.institutions(10),
  });
  const industries = useQuery({
    queryKey: ["reportIndustries"],
    queryFn: api.reportIndustries,
  });

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
        width: 110,
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
      { field: "institution", headerName: "机构", width: 130 },
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
            <span className={meta.cls}>
              {meta.label}
              {d.prev_rating && d.rating_change !== "first" && (
                <span className="ml-1 text-[var(--color-muted)] text-xs">
                  {d.prev_rating}→{d.rating}
                </span>
              )}
            </span>
          );
        },
      },
      {
        field: "has_forecast",
        headerName: "预测",
        width: 70,
        cellRenderer: (p: { value: boolean }) =>
          p.value ? <span className="text-[var(--color-p2)]">有</span> : null,
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
    [],
  );

  function set<K extends keyof ReportFilters>(key: K, value: ReportFilters[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  function search() {
    setPage(1);
    setApplied(form);
  }

  function reset() {
    setForm({});
    setApplied({});
    setPage(1);
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
            theme={gridTheme}
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
          onClick={() => setPage((p) => Math.max(1, p - 1))}
          disabled={page <= 1}
        >
          上一页
        </Button>
        <span className="num text-sm">
          {page} / {maxPage}
        </span>
        <Button
          variant="ghost"
          onClick={() => setPage((p) => Math.min(maxPage, p + 1))}
          disabled={page >= maxPage}
        >
          下一页
        </Button>
        <Badge>每页 {PAGE_SIZE}</Badge>
      </div>
    </div>
  );
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
