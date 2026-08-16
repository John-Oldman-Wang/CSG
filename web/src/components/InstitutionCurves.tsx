import { useQuery } from "@tanstack/react-query";
import ReactECharts from "echarts-for-react";
import { useState } from "react";
import { api } from "@/api/client";
import { Badge, Card, CardTitle, Empty } from "@/components/ui/primitives";
import { useTheme } from "@/lib/theme";
import { pct } from "@/lib/utils";
import type { BuyRule, CurveInstitution, ExitSignal } from "@/types";

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
const EXIT_OPTIONS: { v: ExitSignal; label: string; hint: string }[] = [
  { v: "none", label: "固定持有", hint: "满 N 个交易日卖出，不看后续研报" },
  {
    v: "downgrade",
    label: "同机构下调",
    hint: "跟谁买就听谁的：该机构下调评级后次日离场。实测九年半仅触发 9 次",
  },
  {
    v: "any_downgrade",
    label: "任意机构下调",
    hint: "任何一家下调即离场。触发 374 次（2.1%），但收益反而略降",
  },
  {
    v: "bearish",
    label: "出现非看多评级",
    hint: "持有/中性/减持/卖出 任一出现即离场。A 股这四类合计仅占 0.66%",
  },
];

export function InstitutionCurves({ onPick }: { onPick?: (institution: string) => void }) {
  const { theme } = useTheme();
  const [horizon, setHorizon] = useState(20);
  const [mode, setMode] = useState<"rate" | "excess" | "pnl">("rate");
  const [buy, setBuy] = useState<BuyRule>("bullish");
  const [exitSignal, setExitSignal] = useState<ExitSignal>("none");
  const [roundLot, setRoundLot] = useState(true);

  const { data, isLoading } = useQuery({
    queryKey: ["institutionCurves", horizon, buy, exitSignal, roundLot],
    queryFn: () => api.institutionCurves(horizon, buy, exitSignal, roundLot),
  });

  if (isLoading) return <Empty>加载中</Empty>;
  if (!data || data.institutions.length === 0) return <Empty>样本不足</Empty>;

  const muted = theme === "dark" ? "#9aa0aa" : "#5a6072";
  const grid = theme === "dark" ? "#333842" : "#dcdee5";

  const months = Array.from(
    new Set([data.benchmark, ...data.institutions].flatMap((i) => i.曲线.map((c) => c.月))),
  ).sort();

  const line = (inst: CurveInstitution, isBench: boolean) => {
    // 超额视图用「累计超额 ÷ 占用资金」，与绝对收益率同分母，两条线可叠加比较
    const pick = (c: (typeof inst.曲线)[number]) =>
      mode === "rate"
        ? c.累计收益率 * 100
        : mode === "excess"
          ? (c.累计超额 / inst.所需资金) * 100
          : c.累计盈亏;
    const byMonth = new Map(inst.曲线.map((c) => [c.月, pick(c)]));
    let last: number | null = null;
    const series = months.map((m) => {
      const v = byMonth.get(m);
      if (v != null) last = v;
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
        const month = sorted[0]?.axisValue ?? "";

        // 该月在手股票数：曲线的高度是「赚了多少」，
        // 持股数是「当时压了几只」——两者要一起看才知道钱是怎么来的
        const holdings = new Map(
          [data.benchmark, ...data.institutions].map((i) => {
            const c = i.曲线.find((x) => x.月 === month);
            return [i.机构, c] as const;
          }),
        );
        const body = sorted
          .slice(0, 8)
          .map((p) => {
            const c = holdings.get(p.seriesName);
            const held = c ? `　持股 ${c.月末持股}（月内峰值 ${c.月内峰值持股}）` : "";
            return `${p.seriesName}　${fmt(p.value)}${held}`;
          })
          .join("<br/>");
        return `${month}<br/>${body}${sorted.length > 8 ? "<br/>…" : ""}`;
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
    series: [
      ...data.institutions.map((i) => line(i, false)),
      line(data.benchmark, true),
      // 指数线只在收益率视图下叠加：
      // 超额视图已把指数剥离，再画指数线是重复计量；
      // 盈亏额视图单位是元，与指数百分比无法共轴。
      ...(mode === "rate"
        ? data.indexes.map((ix) => {
            const byMonth = new Map(ix.曲线.map((c) => [c.月, c.涨跌幅 * 100]));
            let last: number | null = null;
            return {
              name: ix.name,
              type: "line",
              data: months.map((m) => {
                const v = byMonth.get(m);
                if (v != null) last = v;
                return last;
              }),
              showSymbol: false,
              z: 8,
              lineStyle: { width: 1.8, type: "dashed", opacity: 0.9 },
              endLabel: { show: true, formatter: ix.name, fontSize: 10 },
            };
          })
        : []),
    ],
  };

  const beatRatio = data.beat_benchmark / data.institutions.length;

  return (
    <Card>
      <CardTitle
        extra={
          <div className="flex items-center gap-3">
            <div className="flex gap-1">
              {(["rate", "excess", "pnl"] as const).map((m) => (
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
                  {m === "rate" ? "收益率" : m === "excess" ? "超额" : "盈亏额"}
                </button>
              ))}
            </div>
            <div className="flex gap-1">
              {[5, 10, 20, 50, 100].map((h) => (
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

      <div className="mb-3 flex flex-wrap items-end gap-4 border-[var(--color-border)] border-b pb-3">
        <label className="block">
          <span className="mb-1 block text-[var(--color-muted)] text-xs">买入信号</span>
          <select
            value={buy}
            onChange={(e) => setBuy(e.target.value as BuyRule)}
            className="rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 text-sm"
          >
            <option value="bullish">看多评级（买入 + 增持，99.33%）</option>
            <option value="strict">仅「买入」评级（77.18%）</option>
          </select>
        </label>

        <label className="block">
          <span className="mb-1 block text-[var(--color-muted)] text-xs">提前离场信号</span>
          <select
            value={exitSignal}
            onChange={(e) => setExitSignal(e.target.value as ExitSignal)}
            title={EXIT_OPTIONS.find((o) => o.v === exitSignal)?.hint}
            className="rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 text-sm"
          >
            {EXIT_OPTIONS.map((o) => (
              <option key={o.v} value={o.v} title={o.hint}>
                {o.label}
              </option>
            ))}
          </select>
        </label>

        <label className="flex cursor-pointer items-center gap-2 pb-1.5 text-sm">
          <input
            type="checkbox"
            checked={roundLot}
            onChange={(e) => setRoundLot(e.target.checked)}
            className="accent-[var(--color-p2)]"
          />
          <span>
            按整手成交
            <span className="ml-1 text-[var(--color-muted)] text-xs">1 手 = 100 股</span>
          </span>
        </label>

        <div className="text-[var(--color-muted)] text-xs">
          触发提前离场 <b className="num text-[var(--color-fg)]">{data.提前离场笔数}</b> 笔 / 共{" "}
          <b className="num text-[var(--color-fg)]">{data.benchmark.笔数}</b> 笔（
          {((data.提前离场笔数 / data.benchmark.笔数) * 100).toFixed(2)}%）
          <br />
          平均实际持有 <b className="num text-[var(--color-fg)]">{data.benchmark.平均持有日}</b>{" "}
          个交易日
        </div>
      </div>

      {exitSignal !== "none" && data.提前离场笔数 / data.benchmark.笔数 < 0.03 && (
        <p className="mb-2 text-[var(--color-p1)] text-xs leading-relaxed">
          ⚠️ 这条卖出规则在 A 股几乎不触发。实测评级分布： 买入 77.18%、增持 22.15%、持有
          0.40%、中性 0.23%、
          <b>卖出 0.03%（九年半共 6 份）、减持 0 份</b>。
          券商不愿得罪覆盖对象，负面观点用「下调」而非「卖出」表达； 而同一机构在 20
          个交易日内下调，九年半仅 9 次。 规则本身没错，是这个市场没有给它可用的信号。
        </p>
      )}

      <p className="mb-2 text-[var(--color-muted)] text-xs leading-relaxed">
        {data.entry_rule}。
        {data.round_lot && (
          <>
            {" "}
            按 A 股整手成交（1 手 = 100 股）， 其中 <b className="num">{data.买不起笔数}</b>{" "}
            笔因 1 万元买不起 1 手而
            <b>完全无法建仓</b>（这些股票股价多在百元以上）； 可建仓部分向下取整后实投均值{" "}
            <b className="num">{data.实投均值.toFixed(0)}</b> 元。
            <span className="text-[var(--color-p1)]">
              {" "}
              ⚠️ 单笔金额因此成为一个隐含筛选器——改成 5 万一笔会买进更多高价股， 结果未必更好。
            </span>
          </>
        )}
        <br />
        离场：{data.exit_rule}，每份投入 1 万元。
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

      {mode === "rate" && (
        <p className="mb-2 text-[var(--color-p1)] text-xs leading-relaxed">
          虚线为指数<b>满仓买入持有</b>，与策略曲线<b>不是同一口径，不可直接比高低</b>：
          指数是一直满仓，策略只在有研报时建仓，平均并发仅为峰值的{" "}
          {((data.benchmark.平均并发 / data.benchmark.峰值并发) * 100).toFixed(0)}%，
          八成时间钱是闲着的。策略跑输指数不等于选股差，可能只是仓位低—— 要看选股能力，请切到
          <b>「超额」</b>视图（同窗口个股收益 − 指数收益）。
        </p>
      )}
      {mode === "excess" && (
        <p className="mb-2 text-[var(--color-muted)] text-xs leading-relaxed">
          <b>同窗口超额</b>：每笔交易的收益 − 同一区间指数（{data.bench_index}）的收益，
          再除以占用资金。它把市场涨跌整个剥离，剩下的才是选股贡献——
          这是唯一能回答「研报有没有价值」的口径。
        </p>
      )}

      <ReactECharts option={option} style={{ height: 420 }} notMerge />

      <div className="mt-3 flex flex-wrap items-center gap-2 border-[var(--color-border)] border-t pt-3">
        <Badge>
          {data.institutions.length} 家（≥{data.min_trades} 笔）
        </Badge>
        <Badge>
          基准年化 {data.benchmark.年化收益率 != null ? pct(data.benchmark.年化收益率, 2) : "—"}
          （{data.benchmark.笔数} 笔 / {data.benchmark.年数} 年）
        </Badge>
        <Badge
          title={`同一时间持股数量最大值，出现在 ${data.benchmark.峰值日期 ?? "—"}；平均 ${data.benchmark.平均并发} 只、中位 ${data.benchmark.中位并发} 只`}
        >
          同时持股峰值 {data.benchmark.峰值并发} 只
        </Badge>
        <Badge
          title={`现金流最低点 ${data.benchmark.所需资金日期 ?? "—"}；粗口径峰值×1万为 ${(data.benchmark.占用资金 / 10000).toFixed(0)} 万，差额即已实现利润的自我供给`}
        >
          实需资金 {(data.benchmark.所需资金 / 10000).toFixed(0)} 万（周转{" "}
          {data.benchmark.周转次数} 次）
        </Badge>
        <Badge title="平均并发 ÷ 峰值并发。低 = 大部分时间资金闲置 = 总收益被稀释">
          资金利用率 {pct(data.benchmark.资金利用率, 1)}
        </Badge>
        <Badge tone={beatRatio > 0.65 ? "P2" : "warn"}>
          年化跑赢基准 {data.beat_benchmark}/{data.institutions.length}
        </Badge>
        <Badge
          tone={(data.benchmark.年化超额 ?? 0) > 0 ? "P2" : "warn"}
          title="剥离市场涨跌后的选股贡献"
        >
          基准年化超额 {data.benchmark.年化超额 != null ? pct(data.benchmark.年化超额, 2) : "—"}
        </Badge>
        {data.indexes.map((ix) => (
          <Badge key={ix.code} title={`区间 ${pct(ix.区间涨跌, 1)}`}>
            {ix.name} 年化 {ix.年化 != null ? pct(ix.年化, 2) : "—"}
          </Badge>
        ))}
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
              title={`${i.笔数} 笔 / ${i.年数} 年 · 年化 ${i.年化收益率 != null ? (i.年化收益率 * 100).toFixed(2) : "—"}% · 实需 ${(i.所需资金 / 10000).toFixed(1)} 万（同时持股峰值 ${i.峰值并发} 只 @ ${i.峰值日期 ?? "—"}） · 胜率 ${(i.胜率 * 100).toFixed(1)}% · 盈亏比 ${i.盈亏比?.toFixed(2) ?? "—"} · 回撤 ${(i.最大回撤 * 100).toFixed(1)}%`}
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
