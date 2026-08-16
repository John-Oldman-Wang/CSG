import { useQuery } from "@tanstack/react-query";
import ReactECharts from "echarts-for-react";
import { useState } from "react";
import { api } from "@/api/client";
import { Badge, Card, CardTitle, Empty } from "@/components/ui/primitives";
import { useTheme } from "@/lib/theme";
import { pct } from "@/lib/utils";
import type { CurveInstitution } from "@/types";

/**
 * 「跟着某机构的每一份研报买」的资金曲线，按机构叠加。
 *
 * 入场规则见后端 ENTRY_CTE：发布次日开盘价买入，开盘涨停顺延，
 * 连续涨停超 5 日放弃该笔。持有 N 个交易日按收盘价卖出。
 *
 * **这张图必须带基准线。** 2017–2026 市场整体上行，绝对收益人人为正，
 * 25 家里 23 家赚钱看起来像「研报普遍有用」。真正的问题是
 * 「挑这家」比「谁的都买」好多少——实测按年化跑赢基准 10/25，
 * 与抛硬币无实质差别。
 *
 * 默认显示**累计收益率**而非累计盈亏：发研报多的机构投入也多，
 * 终点金额自然更高，按金额排名等于按研报数量排名。
 */
export function InstitutionCurves({ onPick }: { onPick?: (institution: string) => void }) {
  const { theme } = useTheme();
  const [horizon, setHorizon] = useState(20);
  const [mode, setMode] = useState<"rate" | "pnl">("rate");

  const { data, isLoading } = useQuery({
    queryKey: ["institutionCurves", horizon],
    queryFn: () => api.institutionCurves(horizon),
  });

  if (isLoading) return <Empty>加载中</Empty>;
  if (!data || data.institutions.length === 0) return <Empty>样本不足</Empty>;

  const muted = theme === "dark" ? "#9aa0aa" : "#5a6072";
  const grid = theme === "dark" ? "#333842" : "#dcdee5";

  const months = Array.from(
    new Set([data.benchmark, ...data.institutions].flatMap((i) => i.曲线.map((c) => c.月))),
  ).sort();

  const line = (inst: CurveInstitution, isBench: boolean) => {
    const byMonth = new Map(
      inst.曲线.map((c) => [c.月, mode === "rate" ? c.累计收益率 : c.累计盈亏]),
    );
    let last: number | null = null;
    const series = months.map((m) => {
      const v = byMonth.get(m);
      if (v != null) last = mode === "rate" ? v * 100 : v;
      return last;
    });
    return {
      name: inst.机构,
      type: "line",
      data: series,
      showSymbol: false,
      z: isBench ? 10 : 1,
      lineStyle: isBench
        ? { width: 3, color: theme === "dark" ? "#e8e9ec" : "#2b2f3a" }
        : { width: 1.2, opacity: 0.55 },
      emphasis: { lineStyle: { width: 2.8, opacity: 1 }, focus: "series" },
      endLabel: isBench
        ? { show: true, formatter: "基准", color: muted, fontSize: 11 }
        : undefined,
    };
  };

  const option = {
    backgroundColor: "transparent",
    grid: { left: 66, right: 120, top: 16, bottom: 56 },
    tooltip: {
      trigger: "axis",
      // 25 条线全列会糊满屏幕，只显示排名靠前的
      formatter: (ps: { axisValue: string; seriesName: string; value: number }[]) => {
        const sorted = [...ps].filter((p) => p.value != null).sort((a, b) => b.value - a.value);
        const fmt = (v: number) =>
          mode === "rate" ? `${v.toFixed(2)}%` : `${(v / 10000).toFixed(1)}万`;
        const head = `${sorted[0]?.axisValue ?? ""}`;
        const body = sorted
          .slice(0, 8)
          .map((p) => `${p.seriesName}　${fmt(p.value)}`)
          .join("<br/>");
        return `${head}<br/>${body}${sorted.length > 8 ? "<br/>…" : ""}`;
      },
    },
    legend: {
      type: "scroll",
      bottom: 0,
      textStyle: { color: muted, fontSize: 10 },
      pageTextStyle: { color: muted },
    },
    xAxis: {
      type: "category",
      data: months,
      axisLabel: { color: muted, fontSize: 10 },
      axisLine: { lineStyle: { color: grid } },
    },
    yAxis: {
      type: "value",
      name: mode === "rate" ? "累计收益率(%)" : "累计盈亏(元)",
      nameTextStyle: { color: muted, fontSize: 10 },
      axisLabel: { color: muted, fontSize: 10 },
      splitLine: { lineStyle: { color: grid, type: "dashed" } },
    },
    series: [...data.institutions.map((i) => line(i, false)), line(data.benchmark, true)],
  };

  const beatRatio = data.beat_benchmark / data.institutions.length;

  return (
    <Card>
      <CardTitle
        extra={
          <div className="flex items-center gap-3">
            <div className="flex gap-1">
              {(["rate", "pnl"] as const).map((m) => (
                <button
                  type="button"
                  key={m}
                  onClick={() => setMode(m)}
                  className={`rounded border px-2 py-0.5 text-xs ${
                    mode === m
                      ? "border-[var(--color-p2)] text-[var(--color-p2)]"
                      : "border-[var(--color-border)] text-[var(--color-muted)]"
                  }`}
                >
                  {m === "rate" ? "收益率" : "盈亏额"}
                </button>
              ))}
            </div>
            <div className="flex gap-1">
              {[20, 50, 100].map((h) => (
                <button
                  type="button"
                  key={h}
                  onClick={() => setHorizon(h)}
                  className={`rounded border px-2 py-0.5 text-xs ${
                    horizon === h
                      ? "border-[var(--color-p2)] text-[var(--color-p2)]"
                      : "border-[var(--color-border)] text-[var(--color-muted)]"
                  }`}
                >
                  {h}日
                </button>
              ))}
            </div>
          </div>
        }
      >
        跟着每家机构买的累计曲线
      </CardTitle>

      <p className="mb-2 text-[var(--color-muted)] text-xs leading-relaxed">
        {data.entry_rule}；持有 {horizon} 个交易日按收盘价卖出，每份投入 1 万元。
        <br />
        <b>收益率的分母是「峰值同时持仓 × 1 万」</b>，不是「1 万 × 笔数」——
        持有期满卖出后资金回笼，下一份研报用的是同一笔钱。 按笔数累加会把基准的本金算成 18,105
        万，实际只需 {(data.benchmark.占用资金 / 10000).toFixed(0)} 万。
        <br />
        粗线为<b>基准：不挑机构，每一份研报都买</b>。
        缺了它这张图会误导——市场整体上行时绝对收益人人为正，
        真正的问题是「挑这家」比「谁的都买」好多少。
        {mode === "pnl" && (
          <>
            <br />
            <span className="text-[var(--color-p1)]">
              ⚠️ 盈亏额视图下，发研报多的机构投入也多、终点自然更高，
              按金额排名约等于按研报数量排名。跨机构比较请切回收益率。
            </span>
          </>
        )}
      </p>

      <ReactECharts option={option} style={{ height: 420 }} notMerge />

      <div className="mt-3 flex flex-wrap items-center gap-2 border-[var(--color-border)] border-t pt-3">
        <Badge>
          {data.institutions.length} 家（≥{data.min_trades} 笔）
        </Badge>
        <Badge>
          基准年化 {data.benchmark.年化收益率 != null ? pct(data.benchmark.年化收益率, 2) : "—"}
          （{data.benchmark.笔数} 笔 / {data.benchmark.年数} 年）
        </Badge>
        <Badge>
          基准占用 {(data.benchmark.占用资金 / 10000).toFixed(0)} 万（峰值并发{" "}
          {data.benchmark.峰值并发}，周转 {data.benchmark.周转次数} 次）
        </Badge>
        <Badge tone={beatRatio > 0.65 ? "P2" : "warn"}>
          年化跑赢基准 {data.beat_benchmark}/{data.institutions.length}
        </Badge>
      </div>

      <p className="mt-2 text-[var(--color-p1)] text-xs leading-relaxed">
        {beatRatio > 0.65
          ? "多数机构跑赢「全买」基准，说明挑选机构这个动作本身可能创造价值。"
          : `跑赢基准的比例 ${(beatRatio * 100).toFixed(0)}%，与抛硬币无实质差别——
             这意味着「挑哪家机构」几乎不改变结果，各家曲线的分散更像噪音而非能力。
             要判断某家是否真有能力，看的不是它在这张图上的高度，而是它能否在
             发现期与验证期都排在前面（见上方斜率图：前 1/3 留存率 0.333 = 随机）。`}
      </p>

      {onPick && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {data.institutions.map((i) => (
            <button
              type="button"
              key={i.机构}
              onClick={() => onPick(i.机构)}
              className="rounded border border-[var(--color-border)] px-2 py-0.5 text-xs hover:border-[var(--color-p2)] hover:text-[var(--color-p2)]"
              title={`${i.笔数} 笔 / ${i.年数} 年 · 年化 ${i.年化收益率 != null ? (i.年化收益率 * 100).toFixed(2) : "—"}% · 占用 ${(i.占用资金 / 10000).toFixed(0)} 万（峰值并发 ${i.峰值并发}） · 胜率 ${(i.胜率 * 100).toFixed(1)}% · 盈亏比 ${i.盈亏比?.toFixed(2) ?? "—"} · 回撤 ${(i.最大回撤 * 100).toFixed(1)}%`}
            >
              {i.机构}{" "}
              <span
                className="num"
                style={{
                  // 与后端 beat_benchmark 同口径：按年化比，不按累计
                  color:
                    (i.年化收益率 ?? -1) > (data.benchmark.年化收益率 ?? -1)
                      ? "var(--color-up)"
                      : "var(--color-down)",
                }}
              >
                {i.年化收益率 != null ? pct(i.年化收益率, 2) : "—"}
              </span>
            </button>
          ))}
        </div>
      )}
    </Card>
  );
}
