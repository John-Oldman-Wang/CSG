import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, DataLockedError } from "@/api/client";
import { Badge, Card, CardTitle, DataLocked, Empty } from "@/components/ui/primitives";

export default function Dashboard() {
  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
  const tasks = useQuery({
    queryKey: ["tasks", "pending"],
    queryFn: () => api.tasks("pending"),
  });
  const pool = useQuery({ queryKey: ["pool"], queryFn: api.pool });

  if (health.error instanceof DataLockedError) {
    return <DataLocked message={health.error.message} />;
  }

  const overdue = tasks.data?.filter((t) => t.overdue) ?? [];

  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <Card>
        <CardTitle>待办复核</CardTitle>
        <div className="flex items-baseline gap-3">
          <span className="num font-semibold text-3xl">{tasks.data?.length ?? "—"}</span>
          {overdue.length > 0 && <Badge tone="warn">{overdue.length} 项超时</Badge>}
        </div>
        <p className="mt-2 text-[var(--color-muted)] text-xs">
          及时处理率是混合派的核心防自欺指标——持续走低意味着在回避亏损标的
        </p>
        <Link to="/tasks" className="mt-3 inline-block text-sm underline">
          查看全部 →
        </Link>
      </Card>

      <Card>
        <CardTitle>股票池</CardTitle>
        <div className="num font-semibold text-3xl">{pool.data?.total ?? "—"}</div>
        <div className="mt-3 space-y-1">
          {pool.data?.breakdown.slice(0, 6).map((b) => (
            <div key={`${b.theme}-${b.industry_name}`} className="flex justify-between text-xs">
              <span className="text-[var(--color-muted)]">{b.industry_name}</span>
              <span className="num">{b.count}</span>
            </div>
          ))}
        </div>
      </Card>

      <Card>
        <CardTitle>数据状态</CardTitle>
        {health.data ? (
          <div className="space-y-1">
            {health.data.counts.map((c) => (
              <div key={c.name} className="flex justify-between text-xs">
                <span className="text-[var(--color-muted)]">{c.name}</span>
                <span className="num">{c.rows.toLocaleString()}</span>
              </div>
            ))}
          </div>
        ) : (
          <Empty>加载中</Empty>
        )}
      </Card>
    </div>
  );
}
