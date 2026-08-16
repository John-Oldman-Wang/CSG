import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  AllCommunityModule,
  type ColDef,
  ModuleRegistry,
  themeQuartz,
} from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import ReactECharts from "echarts-for-react";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, DataLockedError } from "@/api/client";
import { InstitutionCurves } from "@/components/InstitutionCurves";
import { InstitutionPnlSlope } from "@/components/InstitutionPnlSlope";
import { Card, CardTitle, DataLocked, Empty } from "@/components/ui/primitives";
import { useTheme } from "@/lib/theme";
import { pct, trendClass } from "@/lib/utils";
import type { InstitutionTradesResp, TradeRow } from "@/types";

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

const HORIZONS = [20, 50, 100];

type CurvePt = NonNullable<InstitutionTradesResp["curve"]>[number];

/**
 * 机构逐笔复盘 —— 把某机构的每一份研报当作一笔交易列出来。
 *
 * 规则：发布后**第一个交易日**收盘买入，持有 N 个交易日收盘卖出。
 * 发布当日不买——那天的收盘价已经包含研报的影响，用它买入等于
 * 假设你能在研报公开前拿到。
 *
 * **本页的用途是复盘，不是选机构。** 上方的斜率图给出了理由：
 * 机构排名跨期秩相关为负，历史排名不可外推。
 *
 * 逐笔表的真正价值在于打破两种直觉错误：
 *   - 「胜率高才赚钱」——东吴证券胜率 48.5%（低于抛硬币）却总盈利，
 *     靠的是盈亏比 1.43
 *   - 「平均收益为正就该跟」——同一机构中位收益 −0.44%，
 *     即超过一半的研报是亏的，利润来自少数大赢家
 */
export default function InstitutionTrades() {
  const { theme } = useTheme();
  const [params, setParams] = useSearchParams();

  const inst = params.get("inst") ?? "";
  const horizon = Number(params.get("h") ?? 20);

  const set = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v);
    else next.delete(k);
    setParams(next, { replace: true });
  };

  const list = useQuery({
    queryKey: ["institutions"],
    queryFn: () => api.institutionOptions(),
  });

  const chosen = inst || list.data?.[0]?.机构 || "";

  const { data, error, isFetching } = useQuery({
    queryKey: ["institutionTrades", chosen, horizon],
    queryFn: () => api.institutionTrades(chosen, horizon),
    enabled: !!chosen,
    placeholderData: keepPreviousData,
  });

  const [onlyWin, setOnlyWin] = useState<"all" | "win" | "loss">("all");

  const rows = useMemo(() => {
    const t: TradeRow[] = data?.trades ?? [];
    if (onlyWin === "win") return t.filter((r) => r.收益率 > 0);
    if (onlyWin === "loss") return t.filter((r) => r.收益率 <= 0);
    return t;
  }, [data, onlyWin]);

  const columns = useMemo<ColDef<TradeRow>[]>(
    () => [
      {
        field: "发布日",
        width: 115,
        cellClass: "num",
        sort: "desc",
        valueFormatter: (p: { value: string }) => String(p.value).slice(0, 10),
      },
      {
        field: "代码",
        width: 95,
        cellRenderer: (p: { value: string }) => (
          <Link to={`/stocks/${p.value}`} className="num underline underline-offset-2">
            {p.value}
          </Link>
        ),
      },
      { field: "股票", width: 105 },
      { field: "评级", width: 75 },
      {
        field: "买入日",
        width: 115,
        cellClass: "num",
        valueFormatter: (p: { value: string }) => String(p.value).slice(0, 10),
      },
      {
        field: "买入价",
        width: 95,
        type: "numericColumn",
        cellClass: "num",
        valueFormatter: (p: { value: number }) => p.value?.toFixed(2) ?? "—",
      },
      {
        field: "卖出日",
        width: 115,
        cellClass: "num",
        valueFormatter: (p: { value: string }) => String(p.value).slice(0, 10),
      },
      {
        field: "卖出价",
        width: 95,
        type: "numericColumn",
        cellClass: "num",
        valueFormatter: (p: { value: number }) => p.value?.toFixed(2) ?? "—",
      },
      {
        field: "收益率",
        width: 105,
        type: "numericColumn",
        cellRenderer: (p: { data?: TradeRow }) =>
          p.data ? (
            <span className={`num ${trendClass(p.data.收益率)}`}>
              {pct(p.data.收益率, 2)}
              {/* 持有期内除权：按显示价格手算的涨幅会与本列对不上，
                  差额是分红送转——那部分钱确实拿到了，只是不在价格里 */}
              {p.data.除权 && (
                <span
                  className="ml-1 text-[10px] text-[var(--color-p1)]"
                  title="持有期内发生除权除息，按显示价格手算的涨幅与本列不符，差额为分红送转"
                >
                  除权
                </span>
              )}
            </span>
          ) : null,
      },
      {
        field: "盈亏",
        headerName: "盈亏(元)",
        width: 110,
        type: "numericColumn",
        cellRenderer: (p: { value: number }) => (
          <span className={`num ${trendClass(p.value)}`}>
            {p.value >= 0 ? "+" : ""}
            {p.value.toFixed(0)}
          </span>
        ),
      },
      { field: "标题", flex: 1, minWidth: 220, tooltipField: "标题" },
    ],
    [],
  );

  if (error instanceof DataLockedError) return <DataLocked message={error.message} />;

  const s = data?.summary;

  const curveOption = data?.curve
    ? {
        backgroundColor: "transparent",
        grid: { left: 62, right: 20, top: 16, bottom: 30 },
        tooltip: { trigger: "axis" },
        xAxis: {
          type: "category",
          data: (data.curve as CurvePt[]).map((c) => String(c.发布日).slice(0, 10)),
          axisLabel: { color: theme === "dark" ? "#9aa0aa" : "#5a6072", fontSize: 10 },
          axisLine: { lineStyle: { color: theme === "dark" ? "#333842" : "#dcdee5" } },
        },
        yAxis: {
          type: "value",
          name: "累计盈亏(元)",
          nameTextStyle: { color: theme === "dark" ? "#9aa0aa" : "#5a6072", fontSize: 10 },
          axisLabel: { color: theme === "dark" ? "#9aa0aa" : "#5a6072", fontSize: 10 },
          splitLine: {
            lineStyle: { color: theme === "dark" ? "#333842" : "#dcdee5", type: "dashed" },
          },
        },
        series: [
          {
            type: "line",
            data: (data.curve as CurvePt[]).map((c) => Math.round(c.累计)),
            showSymbol: false,
            lineStyle: { width: 1.5, color: "#e04a4a" },
            areaStyle: { opacity: 0.08, color: "#e04a4a" },
          },
        ],
      }
    : null;

  return (
    <div className="space-y-4">
      <InstitutionCurves onPick={(i) => set("inst", i)} />

      <InstitutionPnlSlope />

      <Card>
        <CardTitle
          extra={
            isFetching && <span className="text-[var(--color-muted)] text-xs">加载中…</span>
          }
        >
          逐笔复盘
        </CardTitle>

        <div className="flex flex-wrap items-end gap-4">
          <label className="block">
            <span className="mb-1 block text-[var(--color-muted)] text-xs">机构</span>
            <select
              value={chosen}
              onChange={(e) => set("inst", e.target.value)}
              className={inputCls}
            >
              {(list.data ?? []).map((i) => (
                <option key={i.机构} value={i.机构}>
                  {i.机构}（{i.研报数}）
                </option>
              ))}
            </select>
          </label>

          <label className="block">
            <span className="mb-1 block text-[var(--color-muted)] text-xs">持有期</span>
            <select
              value={horizon}
              onChange={(e) => set("h", e.target.value)}
              className={inputCls}
            >
              {HORIZONS.map((h) => (
                <option key={h} value={h}>
                  {h} 个交易日
                </option>
              ))}
            </select>
          </label>

          <label className="block">
            <span className="mb-1 block text-[var(--color-muted)] text-xs">筛选</span>
            <select
              value={onlyWin}
              onChange={(e) => setOnlyWin(e.target.value as typeof onlyWin)}
              className={inputCls}
            >
              <option value="all">全部</option>
              <option value="win">仅盈利</option>
              <option value="loss">仅亏损</option>
            </select>
          </label>
        </div>

        {s && (
          <>
            <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4 lg:grid-cols-5">
              <Stat label="笔数" value={String(s.笔数)} />
              <Stat
                label="总盈亏"
                value={`${s.总盈亏 >= 0 ? "+" : ""}${(s.总盈亏 / 10000).toFixed(2)} 万`}
                cls={trendClass(s.总盈亏)}
              />
              <Stat
                label="占用资金"
                value={`${(s.占用资金 / 10000).toFixed(0)} 万`}
                hint={`峰值同时持有 ${s.峰值并发} 笔 × 1 万。持有期满卖出后资金回笼，下一笔用的是同一笔钱——不是「1 万 × ${s.笔数} 笔」。平均并发 ${s.平均并发}，资金周转 ${s.周转次数 ?? "—"} 次`}
              />
              <Stat
                label="年化收益"
                value={s.年化收益率 != null ? pct(s.年化收益率, 2) : "—"}
                cls={trendClass(s.年化收益率 ?? 0)}
                hint={`累计 ${s.累计收益率 != null ? pct(s.累计收益率, 1) : "—"} / ${s.年数} 年。这是绝对收益，含市场 beta`}
              />
              <Stat
                label="最大回撤"
                value={pct(s.最大回撤, 1)}
                cls="text-[var(--color-down)]"
                hint="累计盈亏曲线的最大回落，除以占用资金"
              />
              <Stat
                label="胜率"
                value={pct(s.胜率, 1)}
                cls={s.胜率 > 0.5 ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}
                hint="50% 为随机基准"
              />
              <Stat
                label="盈亏比"
                value={s.盈亏比?.toFixed(2) ?? "—"}
                hint="赚钱时平均赚幅 ÷ 亏钱时平均亏幅。胜率低但盈亏比高仍可盈利"
              />
              <Stat
                label="平均收益"
                value={pct(s.平均收益率, 2)}
                cls={trendClass(s.平均收益率)}
              />
              <Stat
                label="中位收益"
                value={pct(s.中位收益率, 2)}
                cls={trendClass(s.中位收益率)}
                hint="中位为负而均值为正，说明利润集中在少数大赢家"
              />
            </div>

            {s.中位收益率 < 0 && s.平均收益率 > 0 && (
              <p className="mt-3 text-[var(--color-p1)] text-xs leading-relaxed">
                ⚠️ 中位收益 {pct(s.中位收益率, 2)} 为负而平均 {pct(s.平均收益率, 2)} 为正：
                <b>超过一半的研报是亏的</b>，全部利润来自少数大赢家。
                这意味着按这家机构的研报逐份买入，最可能的单次结果是亏损——
                只有在能承受长尾、且每一份都买的前提下，总账才为正。
              </p>
            )}
            {s.涨停顺延笔数 > 0 && (
              <p className="mt-1 text-[var(--color-muted)] text-xs">
                {s.涨停顺延笔数} 笔因次日开盘涨停而顺延到之后的交易日买入——
                开盘即涨停时买单排不进去，用那个价格成交是虚构的收益。
              </p>
            )}
            {s.含除权笔数 > 0 && (
              <p className="mt-1 text-[var(--color-muted)] text-xs">
                其中 {s.含除权笔数} 笔在持有期内发生除权除息，
                按表中显示价格手算的涨幅会与「收益率」列不符，差额为分红送转。
              </p>
            )}
          </>
        )}
      </Card>

      {curveOption && (
        <Card>
          <CardTitle
            extra={
              <span className="text-[var(--color-muted)] text-xs">
                按发布日正序累计，非时间等距
              </span>
            }
          >
            资金曲线
          </CardTitle>
          <ReactECharts option={curveOption} style={{ height: 240 }} notMerge />
        </Card>
      )}

      <Card className="p-0">
        {rows.length === 0 ? (
          <Empty>无记录</Empty>
        ) : (
          <div style={{ height: 620 }}>
            <AgGridReact<TradeRow>
              theme={GRID_THEMES[theme]}
              rowData={rows}
              columnDefs={columns}
              defaultColDef={{ sortable: true, resizable: true, filter: true }}
            />
          </div>
        )}
      </Card>

      <Card>
        <p className="text-[var(--color-muted)] text-xs leading-relaxed">
          <b>买入价/卖出价是原始成交价</b>（与券商 App 一致），
          <b>收益率用后复权计算</b>。两者口径不同是刻意的：价格要能对得上你看到的，
          收益率必须正确处理持有期内的除权除息。
          <br />
          发布<b>当日不买</b>——那天的收盘价里已经包含研报的影响，
          用它买入等于假设你能在研报公开前拿到。
          <br />
          窗口未走完的研报<b>不列入</b>：它们尚无结果，填任何数字都是虚构。
        </p>
      </Card>
    </div>
  );
}

function Stat({
  label,
  value,
  cls,
  hint,
}: {
  label: string;
  value: string;
  cls?: string;
  hint?: string;
}) {
  return (
    <div className="rounded border border-[var(--color-border)] px-3 py-2" title={hint}>
      <div className="text-[var(--color-muted)] text-xs">{label}</div>
      <div className={`num mt-0.5 font-medium text-sm ${cls ?? ""}`}>{value}</div>
    </div>
  );
}

const inputCls =
  "rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 text-sm";
