import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  AllCommunityModule,
  type ColDef,
  ModuleRegistry,
  themeQuartz,
} from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import { useMemo, useState } from "react";
import { api, DataLockedError } from "@/api/client";
import { Badge, Button, Card, CardTitle, DataLocked } from "@/components/ui/primitives";
import { useTheme } from "@/lib/theme";
import { pct } from "@/lib/utils";
import type { InstitutionStatRow } from "@/types";

ModuleRegistry.registerModules([AllCommunityModule]);

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

const HORIZONS = [
  { v: 20, label: "20日（约1月）" },
  { v: 60, label: "60日（约3月）" },
  { v: 120, label: "120日（约6月）" },
  { v: 250, label: "250日（约12月）" },
];

const GROUPS = [
  { v: "year", label: "按年" },
  { v: "half", label: "按半年" },
  { v: "quarter", label: "按季度" },
  { v: "all", label: "全期合计" },
] as const;

/**
 * 机构研报胜率统计。
 *
 * **两条不可省略的说明，页面上必须常驻显示：**
 *
 * 1. 胜率以**超额收益**为准（个股收益 − 同月全样本中位数）。
 *    用绝对收益的话，牛市里所有机构都"赢"，那不是研报的功劳。
 *
 * 2. 已排除**窗口未走完**的研报。发布日 + horizon 个交易日若超出
 *    数据末日，该研报尚无结果；纳入统计会依最近行情涨跌而系统性
 *    高估或低估。
 *
 * ⚠️ 本页**不可用于挑选"最准的机构"**：已实测机构排名的跨期
 *    秩相关为 −0.286（比随机更差），历史排名不能外推到未来。
 */
export default function InstitutionStats() {
  const { theme } = useTheme();
  const [horizon, setHorizon] = useState(250);
  const [groupBy, setGroupBy] = useState<"year" | "half" | "quarter" | "all">("year");
  const [minSamples, setMinSamples] = useState(20);

  const { data, error, isFetching } = useQuery({
    queryKey: ["institutionStats", horizon, groupBy, minSamples],
    queryFn: () =>
      api.institutionStats({
        horizon,
        group_by: groupBy,
        min_samples: minSamples,
      }),
    placeholderData: keepPreviousData,
  });

  const columns = useMemo<ColDef<InstitutionStatRow>[]>(
    () => [
      { field: "期间", width: 100, cellClass: "num", sort: "desc" },
      { field: "机构", width: 150 },
      { field: "样本数", width: 100, cellClass: "num", type: "numericColumn" },
      {
        field: "超额中位",
        headerName: "超额收益(中位)",
        width: 150,
        type: "numericColumn",
        cellRenderer: (p: { value: number | null }) => (
          <span
            className="num"
            style={{
              color:
                p.value == null
                  ? undefined
                  : p.value > 0
                    ? "var(--color-up)"
                    : "var(--color-down)",
            }}
          >
            {pct(p.value)}
          </span>
        ),
      },
      {
        field: "胜率",
        width: 110,
        type: "numericColumn",
        cellRenderer: (p: { value: number | null }) => (
          <span
            className="num"
            style={{
              // 50% 是随机基准，低于它意味着不如抛硬币
              color:
                p.value == null
                  ? undefined
                  : p.value > 0.5
                    ? "var(--color-up)"
                    : "var(--color-down)",
            }}
          >
            {p.value == null ? "—" : `${(p.value * 100).toFixed(1)}%`}
          </span>
        ),
      },
      {
        field: "绝对收益中位",
        width: 140,
        type: "numericColumn",
        valueFormatter: (p: { value: number | null }) => pct(p.value),
        cellClass: "num",
      },
      {
        field: "最差",
        width: 110,
        type: "numericColumn",
        valueFormatter: (p: { value: number | null }) => pct(p.value),
        cellClass: "num",
      },
      {
        field: "最好",
        width: 110,
        type: "numericColumn",
        valueFormatter: (p: { value: number | null }) => pct(p.value),
        cellClass: "num",
      },
    ],
    [],
  );

  if (error instanceof DataLockedError) return <DataLocked message={error.message} />;

  const rows = data?.rows ?? [];
  const aboveHalf = rows.filter((r) => (r.胜率 ?? 0) > 0.5).length;

  return (
    <div className="space-y-4">
      <Card className="border-[var(--color-p1)]/40">
        <div className="text-sm">
          <span className="text-[var(--color-p1)]">⚠️ 本页不能用来挑「最准的机构」</span>
          <p className="mt-1 text-[var(--color-muted)] text-xs leading-relaxed">
            已实测：机构排名的跨期秩相关为 <b className="num">−0.286</b>， 发现期排名前 1/3
            的机构在验证期仍居前 1/3 的比例只有 <b className="num">0.143</b>（随机基准 0.333）——
            <b>历史排名不但不可复现，还呈负相关</b>。
            本页的用途是查看历史分布与样本量，不是外推未来。
          </p>
        </div>
      </Card>

      <Card>
        <CardTitle
          extra={
            data?.coverage && (
              <span className="text-[var(--color-muted)] text-xs">
                研报库 {data.coverage.研报总数.toLocaleString()} 条 · 行情至{" "}
                {data.coverage.行情末日}
              </span>
            )
          }
        >
          机构研报胜率
        </CardTitle>

        <div className="flex flex-wrap items-end gap-4">
          <label className="block">
            <span className="mb-1 block text-[var(--color-muted)] text-xs">考察窗口</span>
            <select
              value={horizon}
              onChange={(e) => setHorizon(Number(e.target.value))}
              className={inputCls}
            >
              {HORIZONS.map((h) => (
                <option key={h.v} value={h.v}>
                  {h.label}
                </option>
              ))}
            </select>
          </label>

          <label className="block">
            <span className="mb-1 block text-[var(--color-muted)] text-xs">时间维度</span>
            <select
              value={groupBy}
              onChange={(e) => setGroupBy(e.target.value as typeof groupBy)}
              className={inputCls}
            >
              {GROUPS.map((g) => (
                <option key={g.v} value={g.v}>
                  {g.label}
                </option>
              ))}
            </select>
          </label>

          <label className="block">
            <span className="mb-1 block text-[var(--color-muted)] text-xs">最小样本数</span>
            <input
              type="number"
              min={5}
              value={minSamples}
              onChange={(e) => setMinSamples(Math.max(5, Number(e.target.value)))}
              className={`${inputCls} w-24`}
            />
          </label>

          <Button variant="ghost" onClick={() => setMinSamples(20)}>
            重置
          </Button>

          {isFetching && <span className="text-[var(--color-muted)] text-xs">加载中…</span>}

          <div className="ml-auto flex items-center gap-2 text-sm">
            <Badge>{rows.length} 组</Badge>
            <Badge tone={aboveHalf > rows.length / 2 ? "P2" : "warn"}>
              胜率过半 {aboveHalf}/{rows.length}
            </Badge>
          </div>
        </div>

        <p className="mt-3 text-[var(--color-muted)] text-xs leading-relaxed">
          胜率以<b>超额收益</b>为准（个股收益 − 同月全样本中位数）——
          用绝对收益的话，牛市里所有机构都「赢」，那不是研报的功劳。
          <br />
          已排除<b>窗口未走完</b>的研报：发布日 + {horizon} 个交易日若超出数据末日，
          该研报尚无结果，纳入统计会依最近行情涨跌而系统性高估或低估。
        </p>
      </Card>

      <Card className="p-0">
        <div style={{ height: 620 }}>
          <AgGridReact<InstitutionStatRow>
            theme={GRID_THEMES[theme]}
            rowData={rows}
            columnDefs={columns}
            defaultColDef={{ sortable: true, resizable: true, filter: true }}
            overlayNoRowsTemplate="<span style='color:#9aa0aa'>无满足样本量要求的分组</span>"
          />
        </div>
      </Card>
    </div>
  );
}

const inputCls =
  "rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 text-sm";
