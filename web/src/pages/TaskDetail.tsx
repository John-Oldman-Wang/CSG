import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, DataLockedError } from "@/api/client";
import { Badge, Button, Card, CardTitle, DataLocked, Empty } from "@/components/ui/primitives";
import type { Verdict } from "@/types";

/**
 * 复核任务详情 —— 方法论在界面上的落点。
 *
 * 三条硬性设计（见 docs/METHODOLOGY.md ⑥）：
 *
 * 1. **默认隐藏持仓成本与浮盈亏**。切断沉没成本对判断的干扰，
 *    把「我亏了 20% 要不要割」重构为「这是不是我今天愿意持有的资产」。
 *    可手动展开，但必须是主动动作。
 *
 * 2. **「事实」与「待你判断」在视觉上分区**。前者系统提供，后者人回答。
 *    这条边界若在界面上模糊，久之会把系统整理的材料误当成系统的结论。
 *
 * 3. **信息不足必须填写下次复核日期**。不允许无限期挂起——
 *    那是回避面对亏损标的最常见的形式。
 */

const VERDICTS: { key: Verdict; label: string; hint: string }[] = [
  { key: "sentiment", label: "情绪面", hint: "假设未被动摇，价格与价值背离扩大" },
  { key: "fundamental", label: "价值面", hint: "假设已被削弱或证伪" },
  { key: "insufficient", label: "信息不足", hint: "无法判断，须设定下次复核时点" },
];

export default function TaskDetail() {
  const { taskId = "" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();

  const {
    data: task,
    error,
    isLoading,
  } = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => api.task(taskId),
  });

  const [verdict, setVerdict] = useState<Verdict | null>(null);
  const [wouldRebuy, setWouldRebuy] = useState<boolean | null>(null);
  const [reasoning, setReasoning] = useState("");
  const [nextReview, setNextReview] = useState("");
  const [showCost, setShowCost] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const submit = useMutation({
    mutationFn: () =>
      api.conclude(taskId, {
        verdict: verdict as Verdict,
        would_rebuy: wouldRebuy as boolean,
        reasoning,
        next_review_date: verdict === "insufficient" ? nextReview : null,
      }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["tasks"] });
      if (res.warning) window.alert(res.warning);
      navigate("/tasks");
    },
    onError: (e: Error) => setFormError(e.message),
  });

  if (error instanceof DataLockedError) return <DataLocked message={error.message} />;
  if (isLoading) return <Empty>加载中</Empty>;
  if (!task) return <Empty>任务不存在</Empty>;

  const wl = task.watchlist;

  function handleSubmit() {
    setFormError(null);
    if (!verdict) return setFormError("请选择结论");
    if (wouldRebuy === null) return setFormError("必须回答「是否会重新买入」");
    if (!reasoning.trim()) return setFormError("请填写判断依据");
    if (verdict === "insufficient" && !nextReview) {
      return setFormError("信息不足时必须指定下次复核日期，不允许无限期挂起");
    }
    submit.mutate();
  }

  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <div className="flex items-center gap-3">
        <Badge tone={task.severity}>{task.severity}</Badge>
        <span className="num text-sm">{task.code}</span>
        <span className="font-medium">{task.name}</span>
        {task.overdue && <Badge tone="warn">超时未处理</Badge>}
      </div>
      <h2 className="font-medium text-lg">{task.title}</h2>

      {/* ── 事实：系统整理 ───────────────────────────────── */}
      <Card>
        <CardTitle extra={<span className="text-[var(--color-muted)] text-xs">系统整理</span>}>
          事实
        </CardTitle>
        <dl className="space-y-2">
          {task.context?.facts.map(([k, v]) => (
            <div key={k} className="grid grid-cols-[8rem_1fr] gap-3 text-sm">
              <dt className="text-[var(--color-muted)]">{k}</dt>
              <dd className="whitespace-pre-wrap">{v}</dd>
            </div>
          ))}
        </dl>
      </Card>

      {wl?.falsification && (
        <Card>
          <CardTitle>当初写下的证伪条件</CardTitle>
          <ol className="list-inside list-decimal space-y-1 text-sm">
            {wl.falsification
              .split("；")
              .filter(Boolean)
              .map((c) => (
                <li key={c}>{c.trim()}</li>
              ))}
          </ol>
          <p className="mt-2 text-[var(--color-muted)] text-xs">
            监控的对象是假设，不是价格。请逐条对照。
          </p>
        </Card>
      )}

      {/* 成本默认隐藏 —— 切断沉没成本对判断的干扰 */}
      <Card>
        <div className="flex items-center justify-between">
          <span className="text-[var(--color-muted)] text-sm">持仓成本与浮盈亏已隐藏</span>
          <Button variant="ghost" onClick={() => setShowCost(!showCost)}>
            {showCost ? "重新隐藏" : "仍要查看"}
          </Button>
        </div>
        {showCost && (
          <div className="mt-3 text-sm">
            <p className="text-[var(--color-p1)]">
              ⚠️ 成本价不应参与本次判断。你要回答的是「这是不是我今天愿意持有的资产」，
              而不是「我亏了多少」。
            </p>
            {wl?.target_price && <p className="num mt-2">目标价 {wl.target_price}</p>}
          </div>
        )}
      </Card>

      {/* ── 待你判断：人回答 ─────────────────────────────── */}
      <Card className="border-[var(--color-p1)]/30">
        <CardTitle extra={<span className="text-[var(--color-muted)] text-xs">由你回答</span>}>
          待你判断
        </CardTitle>

        <ol className="mb-4 list-inside list-decimal space-y-1 text-sm">
          {task.context?.questions.map((q) => (
            <li key={q}>{q}</li>
          ))}
        </ol>

        <div className="space-y-4 border-[var(--color-border)] border-t pt-4">
          <div>
            <p className="mb-2 text-sm">结论</p>
            <div className="flex flex-wrap gap-2">
              {VERDICTS.map((v) => (
                <button
                  key={v.key}
                  type="button"
                  onClick={() => setVerdict(v.key)}
                  title={v.hint}
                  className={`rounded-md border px-3 py-1.5 text-sm ${
                    verdict === v.key
                      ? "border-[var(--color-fg)] bg-[var(--color-surface)]"
                      : "border-[var(--color-border)] text-[var(--color-muted)]"
                  }`}
                >
                  {v.label}
                </button>
              ))}
            </div>
            {verdict && (
              <p className="mt-1 text-[var(--color-muted)] text-xs">
                {VERDICTS.find((v) => v.key === verdict)?.hint}
              </p>
            )}
          </div>

          {/* 强制必答题：切断沉没成本 */}
          <div>
            <p className="mb-2 text-sm">
              以今天的价格、今天掌握的信息，我会重新买入吗？
              <span className="ml-1 text-[var(--color-p0)]">*</span>
            </p>
            <div className="flex gap-2">
              {[
                { v: true, label: "会" },
                { v: false, label: "不会" },
              ].map((o) => (
                <button
                  key={o.label}
                  type="button"
                  onClick={() => setWouldRebuy(o.v)}
                  className={`rounded-md border px-4 py-1.5 text-sm ${
                    wouldRebuy === o.v
                      ? "border-[var(--color-fg)] bg-[var(--color-surface)]"
                      : "border-[var(--color-border)] text-[var(--color-muted)]"
                  }`}
                >
                  {o.label}
                </button>
              ))}
            </div>
            {wouldRebuy === false && (
              <p className="mt-1 text-[var(--color-p1)] text-xs">
                若不会重新买入，那么继续持有的理由是什么？请写进下方依据。
              </p>
            )}
          </div>

          {verdict === "insufficient" && (
            <div>
              <p className="mb-2 text-sm">
                下次复核日期<span className="ml-1 text-[var(--color-p0)]">*</span>
              </p>
              <input
                type="date"
                value={nextReview}
                onChange={(e) => setNextReview(e.target.value)}
                className="rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-1.5 text-sm"
              />
              <p className="mt-1 text-[var(--color-muted)] text-xs">
                不允许无限期挂起——那是回避面对亏损标的最常见的形式
              </p>
            </div>
          )}

          <div>
            <p className="mb-2 text-sm">判断依据</p>
            <textarea
              value={reasoning}
              onChange={(e) => setReasoning(e.target.value)}
              rows={4}
              placeholder="依据是什么？哪条证伪条件被触发或未被触发？"
              className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-sm"
            />
          </div>

          {formError && <p className="text-[var(--color-p0)] text-sm">{formError}</p>}

          <Button onClick={handleSubmit} disabled={submit.isPending}>
            {submit.isPending ? "提交中…" : "提交结论"}
          </Button>
          <p className="text-[var(--color-muted)] text-xs">
            结论会进入复盘统计：及时处理率、结论分布、「情绪面」判定的事后正确率。
            这几个数字是混合派唯一的防自欺机制。
          </p>
        </div>
      </Card>
    </div>
  );
}
