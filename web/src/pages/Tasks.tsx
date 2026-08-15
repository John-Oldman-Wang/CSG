import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, DataLockedError } from "@/api/client";
import { Badge, Card, DataLocked, Empty } from "@/components/ui/primitives";

const TABS = [
  { key: "pending", label: "待处理" },
  { key: "notified", label: "已推送" },
  { key: "concluded", label: "已结论" },
  { key: "all", label: "全部" },
];

export default function Tasks() {
  const [status, setStatus] = useState("pending");
  const { data, error, isLoading } = useQuery({
    queryKey: ["tasks", status],
    queryFn: () => api.tasks(status),
  });

  if (error instanceof DataLockedError) return <DataLocked message={error.message} />;

  return (
    <div className="space-y-4">
      <div className="flex gap-1">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            onClick={() => setStatus(t.key)}
            className={`rounded px-3 py-1.5 text-sm ${
              status === t.key
                ? "bg-[var(--color-surface)]"
                : "text-[var(--color-muted)] hover:text-[var(--color-fg)]"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {isLoading && <Empty>加载中</Empty>}
      {data?.length === 0 && (
        <Empty>
          暂无任务。事件由 <code>csg ev detect</code> 扫描产生， 经策略转为复核任务。
        </Empty>
      )}

      <div className="space-y-2">
        {data?.map((t) => (
          <Link key={t.task_id} to={`/tasks/${t.task_id}`} className="block">
            <Card className="transition-colors hover:border-[var(--color-muted)]">
              <div className="flex items-start gap-3">
                <Badge tone={t.severity}>{t.severity}</Badge>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="num text-sm">{t.code}</span>
                    <span className="font-medium text-sm">{t.name}</span>
                    {t.overdue && <Badge tone="warn">超时未处理</Badge>}
                  </div>
                  <p className="mt-1 text-[var(--color-muted)] text-sm">{t.title}</p>
                </div>
                <span className="text-[var(--color-muted)] text-xs">
                  {t.due_at?.slice(5, 16)}
                </span>
              </div>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  );
}
