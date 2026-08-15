import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api, DataLockedError } from "@/api/client";
import CandleChart from "@/components/charts/CandleChart";
import { Badge, Card, CardTitle, DataLocked, Empty } from "@/components/ui/primitives";
import { pct, ratio, trendClass } from "@/lib/utils";

export default function StockDetail() {
  const { code = "" } = useParams();
  const stock = useQuery({ queryKey: ["stock", code], queryFn: () => api.stock(code) });
  const quotes = useQuery({ queryKey: ["quotes", code], queryFn: () => api.quotes(code) });
  const fin = useQuery({ queryKey: ["fin", code], queryFn: () => api.financials(code) });
  const reports = useQuery({
    queryKey: ["reports", code],
    queryFn: () => api.reports(code, 30),
  });

  if (stock.error instanceof DataLockedError)
    return <DataLocked message={stock.error.message} />;
  if (stock.isLoading) return <Empty>加载中</Empty>;

  const latest = fin.data?.periods.at(-1);
  // 评级变化才有信息量；绝对评级 94% 是买入，无区分度
  const changes =
    reports.data?.filter((r) => r.prev_rating && r.prev_rating !== r.rating) ?? [];

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <span className="num text-[var(--color-muted)]">{code}</span>
        <h2 className="font-medium text-lg">{stock.data?.basic.name}</h2>
        {stock.data?.basic.industry_name && <Badge>{stock.data.basic.industry_name}</Badge>}
        {stock.data?.watchlist && (
          <Badge tone={stock.data.watchlist.tier === "holding" ? "P1" : "P2"}>
            {stock.data.watchlist.tier === "holding" ? "持仓" : "观察池"}
          </Badge>
        )}
      </div>

      <Card>
        <CardTitle extra={<span className="text-[var(--color-muted)] text-xs">后复权</span>}>
          价格走势
        </CardTitle>
        <CandleChart
          data={quotes.data ?? []}
          markers={changes.map((r) => ({
            time: r.publish_date,
            text: `${r.prev_rating}→${r.rating}`,
          }))}
        />
        <p className="mt-2 text-[var(--color-muted)] text-xs">
          标记为评级调整事件。绝对评级无区分度（买入占 94%），仅变化值得关注。
        </p>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardTitle
            extra={
              latest?.disclosure_date && (
                <span className="text-[var(--color-muted)] text-xs">
                  披露于 {latest.disclosure_date.slice(0, 10)}
                </span>
              )
            }
          >
            财务指标（PIT）
          </CardTitle>
          {latest ? (
            <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
              {[
                ["ROE(TTM)", pct(latest.roe_ttm)],
                ["毛利率", pct(latest.gross_margin_ttm)],
                ["经营现金流/净利润", ratio(latest.cfo_to_ni)],
                ["资产负债率", pct(latest.debt_ratio)],
                ["商誉/净资产", pct(latest.goodwill_to_equity)],
                ["资本开支/营收", pct(latest.capex_to_revenue)],
              ].map(([k, v]) => (
                <div key={k} className="flex justify-between">
                  <span className="text-[var(--color-muted)]">{k}</span>
                  <span className="num">{v}</span>
                </div>
              ))}
              <div className="flex justify-between">
                <span className="text-[var(--color-muted)]">营收同比</span>
                <span className={`num ${trendClass(latest.revenue_yoy)}`}>
                  {pct(latest.revenue_yoy)}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-[var(--color-muted)]">利润同比</span>
                <span className={`num ${trendClass(latest.profit_yoy)}`}>
                  {pct(latest.profit_yoy)}
                </span>
              </div>
            </div>
          ) : (
            <Empty>无财务数据</Empty>
          )}
          {latest && latest.flag_count > 0 && (
            <div className="mt-3 border-[var(--color-border)] border-t pt-3">
              <Badge tone="warn">命中 {latest.flag_count} 项红旗</Badge>
              <p className="mt-1 text-[var(--color-muted)] text-xs">{latest.flag_names}</p>
            </div>
          )}
        </Card>

        <Card>
          <CardTitle>评级调整</CardTitle>
          {changes.length === 0 ? (
            <Empty>区间内无评级调整</Empty>
          ) : (
            <div className="space-y-2">
              {changes.slice(0, 10).map((r) => {
                const isDown =
                  ["买入", "增持", "持有", "中性", "减持", "卖出"].indexOf(r.rating ?? "") >
                  ["买入", "增持", "持有", "中性", "减持", "卖出"].indexOf(r.prev_rating ?? "");
                return (
                  <div
                    key={`${r.publish_date}-${r.institution}`}
                    className="flex gap-3 text-sm"
                  >
                    <span className="num text-[var(--color-muted)] text-xs">
                      {r.publish_date.slice(0, 10)}
                    </span>
                    <span className="flex-1 truncate">{r.institution}</span>
                    <span
                      className={
                        isDown ? "text-[var(--color-p0)]" : "text-[var(--color-muted)]"
                      }
                    >
                      {r.prev_rating} → {r.rating}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
          <p className="mt-2 text-[var(--color-muted)] text-xs">
            下调逆着分析师的激励机制，比上调更可能携带信息——待验证④ 确认
          </p>
        </Card>
      </div>
    </div>
  );
}
