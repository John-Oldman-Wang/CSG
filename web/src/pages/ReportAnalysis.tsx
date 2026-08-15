import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, DataLockedError } from "@/api/client";
import CandleChart from "@/components/charts/CandleChart";
import { Badge, Card, CardTitle, DataLocked, Empty } from "@/components/ui/primitives";
import { pct, ratio, trendClass, yi } from "@/lib/utils";

/**
 * 研报分析页。
 *
 * **两条不可妥协的展示约束：**
 *
 * 1. 隐含价格是**推算值**（预测EPS × 发布日PE），不是研报目标价。
 *    接口不提供目标价——实测其「预测PE」等于发布日股价÷预测EPS。
 *    图上与表中均须标注推算来源，绝不呈现为分析师观点。
 *
 * 2. 财务指标取**研报发布时点已披露**的数据，不是最新。
 *    用今天的财报去评价两年前的研报，是拿他当时不可能知道的信息苛责他。
 */

export default function ReportAnalysis() {
  const { reportId = "" } = useParams();
  const { data, error, isLoading } = useQuery({
    queryKey: ["reportAnalysis", reportId],
    queryFn: () => api.reportAnalysis(reportId),
  });
  const overview = useQuery({
    queryKey: ["overview", data?.report.code],
    queryFn: () => api.stockOverview(data?.report.code as string),
    enabled: !!data?.report.code,
  });

  if (error instanceof DataLockedError) return <DataLocked message={error.message} />;
  if (isLoading) return <Empty>加载中</Empty>;
  if (!data) return <Empty>研报不存在</Empty>;

  const { report, forecasts, quotes, performance, financials_pit } = data;
  const ov = overview.data;
  const latestFin = financials_pit.at(-1);
  const isDowngrade =
    report.prev_rating &&
    RATING_ORDER.indexOf(report.rating ?? "") > RATING_ORDER.indexOf(report.prev_rating);

  // 隐含价格参考线：仅 2024 年后的研报有预测数据
  const priceLines = forecasts
    .filter((f) => f.implied_price)
    .map((f) => ({
      price: f.implied_price as number,
      title: `${f.forecast_year} 推算 ${f.implied_price}`,
      color: "#8b8f98",
    }));

  return (
    <div className="space-y-4">
      {/* ── 公司信息条 ─────────────────────────────── */}
      <Card>
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-2">
          <Link to={`/stocks/${report.code}`} className="flex items-baseline gap-2">
            <span className="num text-[var(--color-muted)]">{report.code}</span>
            <span className="font-medium text-lg">{report.stock_name}</span>
          </Link>
          {report.industry && <Badge>{report.industry}</Badge>}
          {ov?.quote && (
            <>
              <span className={`num font-semibold text-xl ${trendClass(ov.quote.pct_chg)}`}>
                {ov.quote.close?.toFixed(2)}
              </span>
              <span className={`num text-sm ${trendClass(ov.quote.pct_chg)}`}>
                {ov.quote.pct_chg > 0 ? "+" : ""}
                {ov.quote.pct_chg?.toFixed(2)}%
              </span>
            </>
          )}
          <span className="ml-auto text-[var(--color-muted)] text-xs">
            {ov?.quote?.trade_date?.slice(0, 10)}
          </span>
        </div>

        <div className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2 border-[var(--color-border)] border-t pt-3 text-sm md:grid-cols-4 lg:grid-cols-6">
          <Metric
            label="总市值"
            value={ov?.valuation?.total_mv ? yi(ov.valuation.total_mv) : "—"}
          />
          <Metric
            label="流通市值"
            value={ov?.valuation?.circ_mv ? yi(ov.valuation.circ_mv) : "—"}
          />
          <Metric label="市盈率TTM" value={ratio(ov?.valuation?.pe_ttm)} />
          <Metric label="市净率" value={ratio(ov?.valuation?.pb)} />
          <Metric
            label="换手率"
            value={ov?.quote ? `${ov.quote.turnover?.toFixed(2)}%` : "—"}
          />
          <Metric
            label="52周区间"
            value={
              ov?.band?.low_52w && ov?.band?.high_52w
                ? `${ov.band.low_52w.toFixed(1)}–${ov.band.high_52w.toFixed(1)}`
                : "—"
            }
          />
        </div>
        {!ov?.valuation && (
          <p className="mt-2 text-[var(--color-muted)] text-xs">
            估值数据待采集（daily_basic 表为空，由 baostock 提供 PE/PB/PS）
          </p>
        )}
      </Card>

      {/* ── K线 + 研报标记 ─────────────────────────── */}
      <Card>
        <CardTitle
          extra={
            <span className="text-[var(--color-muted)] text-xs">
              后复权 · 竖线为研报发布日
              {priceLines.length > 0 && " · 横线为推算隐含价"}
            </span>
          }
        >
          价格走势
        </CardTitle>
        <CandleChart
          data={quotes}
          height={420}
          markers={[
            {
              time: String(report.publish_date).slice(0, 10),
              text: `${report.institution} ${report.rating ?? ""}`,
              color: isDowngrade ? "#e5484d" : "#8b8f98",
            },
          ]}
          priceLines={priceLines}
        />
        {priceLines.length > 0 && (
          <p className="mt-2 text-[var(--color-p1)] text-xs">
            ⚠️ 横线为<b>推算值</b>（预测EPS × 发布日PE），非研报目标价——
            数据源不提供目标价，其「预测PE」实为发布日股价÷预测EPS。
            该推算的含义是「若估值倍数维持不变，业绩兑现后对应的价格」。
          </p>
        )}
      </Card>

      {/* ── 发布后表现 ─────────────────────────────── */}
      {performance && (
        <Card>
          <CardTitle
            extra={
              <span className="text-[var(--color-muted)] text-xs">
                入场价取发布日后首个交易日收盘
              </span>
            }
          >
            发布后表现
          </CardTitle>
          <div className="grid grid-cols-3 gap-4 md:grid-cols-6">
            {[
              ["20日", performance.ret_20],
              ["60日", performance.ret_60],
              ["120日", performance.ret_120],
              ["250日", performance.ret_250],
              ["期内最大浮盈", performance.max_gain],
              ["期内最大浮亏", performance.max_loss],
            ].map(([label, v]) => (
              <div key={String(label)}>
                <div className="text-[var(--color-muted)] text-xs">{label}</div>
                <div className={`num text-lg ${trendClass(v as number)}`}>
                  {pct(v as number)}
                </div>
              </div>
            ))}
          </div>
          <p className="mt-2 text-[var(--color-muted)] text-xs">
            「最大浮亏」决定这份推荐实际上拿不拿得住——终点收益相同但中途 −45% 与
            −8%，可执行性天差地别
          </p>
        </Card>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        {/* ── 研报详情 ───────────────────────────── */}
        <Card>
          <CardTitle>研报</CardTitle>
          <h3 className="font-medium text-sm leading-relaxed">{report.title}</h3>
          <div className="mt-3 space-y-2 text-sm">
            <Row label="机构" value={report.institution} />
            <Row label="发布日期" value={String(report.publish_date).slice(0, 10)} />
            <Row
              label="评级"
              value={
                report.prev_rating ? (
                  <span className={isDowngrade ? "text-[var(--color-p0)]" : ""}>
                    {report.prev_rating} → {report.rating}
                    {isDowngrade && " （下调）"}
                  </span>
                ) : (
                  <span>
                    {report.rating}
                    <span className="ml-1 text-[var(--color-muted)] text-xs">首次覆盖</span>
                  </span>
                )
              }
            />
            <Row
              label="发布日股价"
              value={data.price_at_publish ? data.price_at_publish.toFixed(2) : "—"}
            />
            <Row label="发布日PE" value={ratio(data.valuation_at_publish?.pe_ttm)} />
          </div>

          {forecasts.length > 0 && (
            <div className="mt-4 border-[var(--color-border)] border-t pt-3">
              <div className="mb-2 text-[var(--color-muted)] text-xs">盈利预测</div>
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-[var(--color-muted)] text-xs">
                    <th className="text-left font-normal">年份</th>
                    <th className="text-right font-normal">预测EPS</th>
                    <th className="text-right font-normal">推算隐含价</th>
                  </tr>
                </thead>
                <tbody>
                  {forecasts.map((f) => (
                    <tr key={f.forecast_year}>
                      <td className="num py-1">{f.forecast_year}</td>
                      <td className="num py-1 text-right">{f.eps?.toFixed(2) ?? "—"}</td>
                      <td className="num py-1 text-right">
                        {f.implied_price?.toFixed(2) ?? "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {report.pdf_url && (
            <a
              href={report.pdf_url}
              target="_blank"
              rel="noreferrer"
              className="mt-3 inline-block text-sm underline"
            >
              查看原文 PDF →
            </a>
          )}
        </Card>

        {/* ── 财务指标（PIT） ─────────────────────── */}
        <Card>
          <CardTitle
            extra={
              latestFin?.disclosure_date && (
                <span className="text-[var(--color-muted)] text-xs">
                  最新可见 {String(latestFin.report_period).slice(0, 10)}
                </span>
              )
            }
          >
            财务指标
          </CardTitle>
          <p className="mb-3 text-[var(--color-p2)] text-xs">
            为研报<b>发布时点已披露</b>的数据，非最新——用今天的财报评价当时的研报，
            是拿他不可能知道的信息苛责他
          </p>
          {latestFin ? (
            <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
              <Row label="ROE(TTM)" value={pct(latestFin.roe_ttm)} />
              <Row label="毛利率" value={pct(latestFin.gross_margin_ttm)} />
              <Row label="净利率" value={pct(latestFin.net_margin_ttm)} />
              <Row label="经营现金流/净利润" value={ratio(latestFin.cfo_to_ni)} />
              <Row
                label="营收同比"
                value={pct(latestFin.revenue_yoy)}
                tone={latestFin.revenue_yoy}
              />
              <Row
                label="利润同比"
                value={pct(latestFin.profit_yoy)}
                tone={latestFin.profit_yoy}
              />
              <Row label="资产负债率" value={pct(latestFin.debt_ratio)} />
              <Row label="商誉/净资产" value={pct(latestFin.goodwill_to_equity)} />
              <Row label="资本开支/营收" value={pct(latestFin.capex_to_revenue)} />
              <Row label="合同负债/营收" value={pct(latestFin.contract_liab_to_revenue)} />
            </div>
          ) : (
            <Empty>该时点无已披露财务数据</Empty>
          )}
          {latestFin && latestFin.flag_count > 0 && (
            <div className="mt-3 border-[var(--color-border)] border-t pt-3">
              <Badge tone="warn">当时已命中 {latestFin.flag_count} 项财务红旗</Badge>
              <p className="mt-1 text-[var(--color-muted)] text-xs">{latestFin.flag_names}</p>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

const RATING_ORDER = ["买入", "增持", "持有", "中性", "减持", "卖出"];

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[var(--color-muted)] text-xs">{label}</div>
      <div className="num">{value}</div>
    </div>
  );
}

function Row({
  label,
  value,
  tone,
}: {
  label: string;
  value: React.ReactNode;
  tone?: number | null;
}) {
  return (
    <div className="flex justify-between gap-3">
      <span className="text-[var(--color-muted)]">{label}</span>
      <span className={`num ${tone !== undefined ? trendClass(tone) : ""}`}>{value}</span>
    </div>
  );
}
