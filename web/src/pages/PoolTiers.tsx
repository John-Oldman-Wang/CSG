import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, DataLockedError } from "@/api/client";
import { Badge, Card, CardTitle, DataLocked, Empty } from "@/components/ui/primitives";
import { pct, trendClass } from "@/lib/utils";

/**
 * 股票池分级 —— 机构投研流程中最值得复制的一条制度。
 *
 * 标的必须逐级晋升，每级有明确的入池条件：
 *   L0 全市场 → L1 基础池 → L2 观察池 → L3 持仓
 *
 * **L1→L2 的门槛是写出证伪条件。** 写不出说明还没想清楚，
 * 此时正确的动作是继续研究而非买入。
 *
 * 页面刻意突出「持仓但未写假设」的标的——那是跳级买入的痕迹，
 * 意味着跌了之后没有判断依据。
 */
export default function PoolTiers() {
  const { data, error, isLoading } = useQuery({
    queryKey: ["poolTiers"],
    queryFn: api.poolTiers,
  });
  const portfolio = useQuery({ queryKey: ["portfolio"], queryFn: api.portfolio });

  if (error instanceof DataLockedError) return <DataLocked message={error.message} />;
  if (isLoading) return <Empty>加载中</Empty>;
  if (!data) return <Empty>无数据</Empty>;

  const skipped = data.coverage.held_not_watched;
  const maxCount = Math.max(...data.tiers.map((t) => t.count));

  return (
    <div className="space-y-4">
      {/* ── 漏斗 ────────────────────────────────── */}
      <Card>
        <CardTitle
          extra={
            <span className="text-[var(--color-muted)] text-xs">
              标的须逐级晋升，不允许跳级
            </span>
          }
        >
          股票池分级
        </CardTitle>
        <div className="space-y-2">
          {data.tiers.map((t) => (
            <div key={t.tier} className="flex items-center gap-3">
              <Badge tone={t.tier === "L3" ? "P1" : "default"}>{t.tier}</Badge>
              <span className="w-16 text-sm">{t.name}</span>
              <div className="h-6 flex-1 overflow-hidden rounded bg-[var(--color-bg)]">
                <div
                  className="flex h-full items-center rounded bg-[var(--color-p2)]/35 px-2"
                  style={{
                    // 对数刻度：L0 五千余只与 L3 数只无法在线性刻度上共存
                    width: `${Math.max(6, (Math.log10(t.count + 1) / Math.log10(maxCount + 1)) * 100)}%`,
                  }}
                >
                  <span className="num font-medium text-xs">{t.count}</span>
                </div>
              </div>
              <span className="hidden w-96 text-[var(--color-muted)] text-xs lg:block">
                {t.desc}
              </span>
            </div>
          ))}
        </div>
        <p className="mt-3 border-[var(--color-border)] border-t pt-3 text-[var(--color-muted)] text-xs">
          {data.promotion_gate}
        </p>
      </Card>

      {/* ── 跳级警告 ─────────────────────────────── */}
      {skipped.length > 0 && (
        <Card className="border-[var(--color-p0)]/40">
          <div className="text-sm">
            <span className="text-[var(--color-p0)]">
              ⚠️ {skipped.length} 只已持仓但未写买入理由与证伪条件
            </span>
            <p className="mt-1 text-[var(--color-muted)] text-xs leading-relaxed">
              这是跳级买入的痕迹。没有事先写下的证伪条件，股价下跌时
              无法判断该加仓还是该止损——因为当初没定标准，事后怎么说都能自圆其说。
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              {skipped.map((code) => (
                <Link key={code} to={`/stocks/${code}`}>
                  <Badge tone="warn">{code}</Badge>
                </Link>
              ))}
            </div>
          </div>
        </Card>
      )}

      {/* ── 持仓与约束 ───────────────────────────── */}
      {portfolio.data?.summary && (
        <Card>
          <CardTitle
            extra={
              <span className={`num text-sm ${trendClass(portfolio.data.summary.pnl_pct)}`}>
                {pct(portfolio.data.summary.pnl_pct)}
              </span>
            }
          >
            L3 持仓
          </CardTitle>

          <div className="mb-3 flex flex-wrap gap-x-6 gap-y-1 text-sm">
            <span className="text-[var(--color-muted)]">
              市值{" "}
              <span className="num text-[var(--color-fg)]">
                {(portfolio.data.summary.total_market_value / 1e4).toFixed(2)} 万
              </span>
            </span>
            <span className="text-[var(--color-muted)]">
              成本{" "}
              <span className="num text-[var(--color-fg)]">
                {(portfolio.data.summary.total_cost / 1e4).toFixed(2)} 万
              </span>
            </span>
            <span className="text-[var(--color-muted)]">
              浮动{" "}
              <span className={`num ${trendClass(portfolio.data.summary.pnl)}`}>
                {(portfolio.data.summary.pnl / 1e4).toFixed(2)} 万
              </span>
            </span>
            <span className="text-[var(--color-muted)]">
              持仓{" "}
              <span className="num text-[var(--color-fg)]">{portfolio.data.summary.count}</span>{" "}
              只（约束 {portfolio.data.summary.count_limit}）
            </span>
          </div>

          <table className="w-full text-sm">
            <thead>
              <tr className="border-[var(--color-border)] border-b text-[var(--color-muted)] text-xs">
                <th className="py-1.5 text-left font-normal">代码</th>
                <th className="py-1.5 text-left font-normal">名称</th>
                <th className="py-1.5 text-left font-normal">行业</th>
                <th className="py-1.5 text-right font-normal">权重</th>
                <th className="py-1.5 text-right font-normal">市值</th>
                <th className="py-1.5 text-right font-normal">盈亏</th>
              </tr>
            </thead>
            <tbody>
              {portfolio.data.holdings.map((h) => (
                <tr key={h.code} className="border-[var(--color-border)]/50 border-b">
                  <td className="num py-1.5">
                    <Link to={`/stocks/${h.code}`} className="underline underline-offset-2">
                      {h.code}
                    </Link>
                  </td>
                  <td className="py-1.5">{h.name}</td>
                  <td className="py-1.5 text-[var(--color-muted)]">{h.industry}</td>
                  <td className="num py-1.5 text-right">
                    {pct(h.weight, 1)}
                    {h.weight > 0.15 && <span className="ml-1 text-[var(--color-p0)]">!</span>}
                  </td>
                  <td className="num py-1.5 text-right">
                    {(h.market_value / 1e4).toFixed(2)} 万
                  </td>
                  <td className={`num py-1.5 text-right ${trendClass(h.pnl_pct)}`}>
                    {pct(h.pnl_pct, 1)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {/* ── 约束违反 ─────────────────────────────── */}
      {portfolio.data?.violations && portfolio.data.violations.length > 0 && (
        <Card className="border-[var(--color-p0)]/40">
          <CardTitle
            extra={
              <span className="text-[var(--color-muted)] text-xs">
                系统无下单权限，提示可被无视——但会被记录
              </span>
            }
          >
            约束违反 {portfolio.data.violations.length} 项
          </CardTitle>
          <div className="space-y-2">
            {portfolio.data.violations.map((v) => (
              <div key={`${v.rule}-${v.target}`} className="flex items-start gap-2 text-sm">
                <span className="text-[var(--color-p0)]">🚫</span>
                <div>
                  <span className="text-[var(--color-muted)]">[{v.rule}]</span> {v.message}
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* ── L2 观察池 ────────────────────────────── */}
      <Card>
        <CardTitle
          extra={
            <span className="text-[var(--color-muted)] text-xs">入池门槛：写出证伪条件</span>
          }
        >
          L2 观察池
        </CardTitle>
        {data.watchlist.length === 0 ? (
          <Empty>观察池为空。系统的所有监控都建立在你写下的假设之上。</Empty>
        ) : (
          <div className="space-y-3">
            {data.watchlist.map((w) => (
              <div key={w.code} className="rounded border border-[var(--color-border)] p-3">
                <div className="flex items-center gap-2">
                  <Link to={`/stocks/${w.code}`} className="num underline underline-offset-2">
                    {w.code}
                  </Link>
                  <span className="font-medium">{w.name}</span>
                  {w.industry && <Badge>{w.industry}</Badge>}
                  {w.in_position && <Badge tone="P1">已建仓</Badge>}
                  <Badge tone={w.falsification_count > 0 ? "P2" : "warn"}>
                    {w.falsification_count} 条证伪条件
                  </Badge>
                  <span className="ml-auto text-[var(--color-muted)] text-xs">
                    {w.added_at?.slice(0, 10)}
                  </span>
                </div>
                {w.thesis && (
                  <p className="mt-2 text-[var(--color-muted)] text-xs leading-relaxed">
                    {w.thesis}
                  </p>
                )}
                {w.falsification && (
                  <ol className="mt-2 list-inside list-decimal space-y-0.5 text-xs">
                    {w.falsification
                      .split("；")
                      .filter(Boolean)
                      .map((c) => (
                        <li key={c} className="text-[var(--color-fg)]">
                          {c.trim()}
                        </li>
                      ))}
                  </ol>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* ── L1 构成 ──────────────────────────────── */}
      <Card>
        <CardTitle>L1 基础池构成</CardTitle>
        <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm md:grid-cols-3">
          {data.l1_breakdown.map((b) => (
            <div key={`${b.theme}-${b.industry_name}`} className="flex justify-between">
              <span className="text-[var(--color-muted)]">{b.industry_name}</span>
              <span className="num">{b.count}</span>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
