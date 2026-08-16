import { useQuery } from "@tanstack/react-query";
import ReactECharts from "echarts-for-react";
import { useState } from "react";
import { api } from "@/api/client";
import { Badge, Card, CardTitle, Empty } from "@/components/ui/primitives";
import { useTheme } from "@/lib/theme";

/**
 * 机构盈亏斜率图 —— 「每份研报投入一万元、持有 N 个交易日」的累计超额盈亏，
 * 发现期与验证期并列，同一机构连线。
 *
 * **这张图的答案是连线的形状，不是任何一根柱子的高度。**
 *
 * 若某几家两期都在上方，那是可复现的信号；若连线大面积交叉，
 * 说明「哪家机构有价值」这个问题没有稳定答案——而实测正是后者：
 * 秩相关 −0.366，前 1/3 留存率 0.333（恰等于随机基准）。
 *
 * 两个刻意的设计决定：
 *
 * 1. **基准用均值而非中位数。** 收益右偏（能涨 200%，最多跌 100%），
 *    以中位数为基准时少数大赢家把均值拉高，18 家里 17 家两期皆盈——
 *    那不是 alpha 是偏度。均值基准使超额严格零和，机构间才可比。
 * 2. **不提供「全期合计」视图。** 只看全期排名会直接引导出
 *    「跟着第一名买」，而数据说那不成立。分期是这张图的全部意义。
 */
export function InstitutionPnlSlope() {
  const { theme } = useTheme();
  const [horizon, setHorizon] = useState(20);
  const { data, isLoading } = useQuery({
    queryKey: ["institutionPnl", horizon],
    queryFn: () => api.institutionPnl(horizon),
  });

  if (isLoading) return <Empty>加载中</Empty>;
  if (!data || data.rows.length === 0) return <Empty>样本不足</Empty>;

  const muted = theme === "dark" ? "#9aa0aa" : "#5a6072";
  const grid = theme === "dark" ? "#333842" : "#dcdee5";
  const up = "#e04a4a";
  const down = "#3aa675";

  const wan = (v: number) => v / 10000;

  const option = {
    backgroundColor: "transparent",
    grid: { left: 70, right: 130, top: 40, bottom: 40 },
    tooltip: {
      trigger: "item",
      formatter: (p: { seriesName: string; data: number[] }) => {
        const row = data.rows.find((r) => r.机构 === p.seriesName);
        if (!row) return "";
        return [
          `<b>${row.机构}</b>`,
          `发现期 #${row.发现排名}　${row.发现累计 >= 0 ? "+" : ""}${wan(row.发现累计).toFixed(2)} 万（${row.发现份数} 份）`,
          `验证期 #${row.验证排名}　${row.验证累计 >= 0 ? "+" : ""}${wan(row.验证累计).toFixed(2)} 万（${row.验证份数} 份）`,
        ].join("<br/>");
      },
    },
    xAxis: {
      type: "category",
      data: ["发现期 2018–2022", "验证期 2023–至今"],
      axisLine: { lineStyle: { color: grid } },
      axisLabel: { color: muted, fontSize: 12 },
      boundaryGap: ["20%", "20%"],
    },
    yAxis: {
      type: "value",
      name: "累计超额盈亏（万元）",
      nameTextStyle: { color: muted, fontSize: 11 },
      axisLine: { show: false },
      splitLine: { lineStyle: { color: grid, type: "dashed" } },
      axisLabel: { color: muted, fontSize: 11 },
    },
    series: data.rows.map((r) => {
      // 着色按**验证期**表现，不按发现期：
      // 若按发现期上色，图会呈现「红线在上、绿线在下」的整齐假象，
      // 掩盖掉交叉本身——而交叉才是这张图要说的事。
      const good = r.验证累计 > 0;
      return {
        name: r.机构,
        type: "line",
        symbol: "circle",
        symbolSize: 7,
        data: [wan(r.发现累计), wan(r.验证累计)],
        lineStyle: { width: 1.6, color: good ? up : down, opacity: 0.75 },
        itemStyle: { color: good ? up : down },
        endLabel: {
          show: true,
          formatter: r.机构,
          color: muted,
          fontSize: 11,
          distance: 6,
        },
        emphasis: { lineStyle: { width: 3, opacity: 1 }, focus: "series" },
      };
    }),
    markLine: { silent: true },
  };

  const s = data.stability;
  const reproducible = s.秩相关 > 0.3 && s["前1/3留存"] > s.随机基准 * 1.5;

  return (
    <Card>
      <CardTitle
        extra={
          <div className="flex items-center gap-2">
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
        }
      >
        每份研报投 1 万元，持有 {horizon} 个交易日
      </CardTitle>

      <p className="mb-2 text-[var(--color-muted)] text-xs leading-relaxed">
        纵轴为<b>累计超额</b>盈亏（个股收益 − 同月全样本<b>均值</b>）。 用中位数作基准会让 18
        家里 17 家两期皆盈——收益右偏，少数大赢家把均值拉高， 那不是 alpha
        是偏度。均值基准下超额严格零和，机构之间才是真正的相对比较。
        <br />
        线的颜色按<b>验证期</b>表现着色（红盈绿亏）。
        <b>要看的是连线的形状，不是任何一根线的高度。</b>
      </p>

      <ReactECharts
        option={option}
        style={{ height: 460 }}
        theme={theme === "dark" ? "dark" : undefined}
        notMerge
      />

      <div className="mt-3 flex flex-wrap items-center gap-2 border-[var(--color-border)] border-t pt-3">
        <Badge>
          {s.机构数} 家（两期各 ≥ {data.min_samples} 份）
        </Badge>
        <Badge tone={s.秩相关 > 0 ? "P2" : "warn"}>秩相关 {s.秩相关}</Badge>
        <Badge tone={s["前1/3留存"] > s.随机基准 ? "P2" : "warn"}>
          前 1/3 留存 {s["前1/3留存"]}（随机 {s.随机基准}）
        </Badge>
        <Badge tone={s.两期皆盈 > s.机构数 / 3 ? "P2" : "warn"}>
          两期皆盈 {s.两期皆盈}/{s.机构数}
        </Badge>
      </div>

      <p
        className={`mt-2 text-xs leading-relaxed ${
          reproducible ? "text-[var(--color-p2)]" : "text-[var(--color-p1)]"
        }`}
      >
        {reproducible
          ? "排名在两期间呈正相关且留存率显著高于随机——存在可复现的机构差异，但仍需更长样本确认。"
          : `排名不可复现：秩相关 ${s.秩相关}（负值意味着比随机更差，呈均值回归），前 1/3 留存率 ${s["前1/3留存"]} 与随机基准 ${s.随机基准} 无实质差别。
             这张图能告诉你「过去谁赚了钱」，不能告诉你「未来跟谁买」——两者之间的桥，数据没有架起来。`}
      </p>
    </Card>
  );
}
