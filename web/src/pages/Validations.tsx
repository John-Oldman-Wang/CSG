import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, DataLockedError } from "@/api/client";
import { Card, CardTitle, DataLocked, Empty } from "@/components/ui/primitives";

/** 验证结果。发现期与验证期并排展示——
 *  只有两期同向的效应才可采信，仅发现期成立的是噪音。 */
export default function Validations() {
  const [runId, setRunId] = useState<string | null>(null);
  const runs = useQuery({ queryKey: ["validations"], queryFn: api.validations });
  const detail = useQuery({
    queryKey: ["validation", runId],
    queryFn: () => api.validation(runId as string),
    enabled: !!runId,
  });

  if (runs.error instanceof DataLockedError) return <DataLocked message={runs.error.message} />;

  return (
    <div className="space-y-4">
      <Card>
        <CardTitle>验证运行历史</CardTitle>
        {runs.data?.length === 0 && (
          <Empty>
            尚无验证记录。运行 <code>csg validate research</code> 后结果会落库。
          </Empty>
        )}
        <div className="space-y-1">
          {runs.data?.map((r) => (
            <button
              key={r.run_id}
              type="button"
              onClick={() => setRunId(r.run_id)}
              className={`flex w-full justify-between rounded px-2 py-1.5 text-left text-sm ${
                runId === r.run_id
                  ? "bg-[var(--color-border)]/50"
                  : "hover:bg-[var(--color-border)]/30"
              }`}
            >
              <span>{r.validation_type}</span>
              <span className="num text-[var(--color-muted)] text-xs">
                {String(r.run_at).slice(0, 16)} · 研报 {r.研报数} · {r.结果行数} 行结果
              </span>
            </button>
          ))}
        </div>
      </Card>

      {detail.data &&
        Object.entries(detail.data.results).map(([view, rows]) => (
          <Card key={view}>
            <CardTitle>{view}</CardTitle>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-[var(--color-border)] border-b text-[var(--color-muted)] text-xs">
                    {Object.keys(rows[0] ?? {}).map((k) => (
                      <th key={k} className="px-2 py-1.5 text-left font-normal">
                        {k}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, i) => (
                    <tr
                      key={String(row.分组 ?? i)}
                      className="border-[var(--color-border)]/50 border-b"
                    >
                      {Object.entries(row).map(([k, v]) => (
                        <td key={k} className="num px-2 py-1.5">
                          {v == null ? "—" : String(v)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        ))}
    </div>
  );
}
