import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/** shadcn 风格的基础件：组件代码留在项目内，可随时改，不是黑盒依赖。 */

export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div
      className={cn(
        "rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function CardTitle({ children, extra }: { children: ReactNode; extra?: ReactNode }) {
  return (
    <div className="mb-3 flex items-center justify-between">
      <h3 className="font-medium text-sm text-[var(--color-fg)]">{children}</h3>
      {extra}
    </div>
  );
}

/**
 * 配色语义（全站统一，不可混用）：
 *
 *   红 --color-up    **数值语义**：高于基准、上涨、胜率过半
 *   绿 --color-down  **数值语义**：低于基准、下跌、胜率不足半数
 *   橙 --color-p1    **警示语义**：需要注意的事件（评级下调、超时任务、约束临界）
 *   红 --color-p0    **仅用于 P0 级别事件**：证伪命中、财报暴雷等须立即处理者
 *   蓝 --color-p2    中性信息
 *   灰               无信息 / 样本不足 / 已失效
 *
 * ⚠️ 红色有两个来源（up 与 p0），语义不同。
 *    在同时展示数值与事件的表格中，事件一律用橙（warn），
 *    把红色让给数值——否则同一张表里两种红分别代表好与坏，必然误读。
 */
const severityStyle: Record<string, string> = {
  P0: "bg-[var(--color-p0)]/15 text-[var(--color-p0)] border-[var(--color-p0)]/40",
  P1: "bg-[var(--color-p1)]/15 text-[var(--color-p1)] border-[var(--color-p1)]/40",
  P2: "bg-[var(--color-p2)]/15 text-[var(--color-p2)] border-[var(--color-p2)]/40",
};

export function Badge({
  children,
  tone = "default",
  className,
}: {
  children: ReactNode;
  tone?: "default" | "P0" | "P1" | "P2" | "warn";
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded border px-1.5 py-0.5 font-medium text-xs",
        tone === "default" && "border-[var(--color-border)] text-[var(--color-muted)]",
        tone === "warn" &&
          "border-[var(--color-p1)]/40 bg-[var(--color-p1)]/15 text-[var(--color-p1)]",
        severityStyle[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

export function Button({
  children,
  onClick,
  variant = "default",
  disabled,
  type = "button",
  className,
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  variant?: "default" | "ghost" | "danger";
  type?: "button" | "submit";
  className?: string;
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "rounded-md px-3 py-1.5 font-medium text-sm transition-colors disabled:opacity-40",
        variant === "default" && "bg-[var(--color-fg)] text-[var(--color-bg)] hover:opacity-90",
        variant === "ghost" &&
          "border border-[var(--color-border)] hover:bg-[var(--color-border)]/40",
        variant === "danger" && "bg-[var(--color-p0)] text-white hover:opacity-90",
        className,
      )}
    >
      {children}
    </button>
  );
}

/** 胜率徽章。
 *
 * 50% 是随机基准——低于它意味着不如抛硬币，故以此为着色分界。
 * 配色沿用 A 股惯例：优于基准用红，劣于基准用绿。
 * 样本量始终并列显示：小样本的胜率不具解读价值，
 * 只给数字而不给样本量会诱导错误判断。
 */
export function WinRateBadge({
  rate,
  samples,
  minSamples = 10,
  label,
}: {
  rate: number | null;
  samples: number;
  minSamples?: number;
  label?: string;
}) {
  const insufficient = samples < minSamples;
  const tone =
    rate == null || insufficient
      ? "border-[var(--color-border)] text-[var(--color-muted)]"
      : rate > 0.5
        ? "border-[var(--color-up)]/40 bg-[var(--color-up)]/10 text-[var(--color-up)]"
        : "border-[var(--color-down)]/40 bg-[var(--color-down)]/10 text-[var(--color-down)]";

  return (
    <span
      title={
        insufficient
          ? `样本仅 ${samples} 条，不足 ${minSamples}，统计量不可靠`
          : `${samples} 条样本`
      }
      className={cn(
        "num inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-xs",
        tone,
        insufficient && "opacity-50",
      )}
    >
      {label && <span className="text-[10px] opacity-70">{label}</span>}
      {rate == null ? "—" : `${(rate * 100).toFixed(0)}%`}
      <span className="text-[10px] opacity-60">({samples})</span>
    </span>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="py-8 text-center text-[var(--color-muted)] text-sm">{children}</div>;
}

/** 数据库被采集锁定时的统一提示。这是已知状态，不是错误。 */
export function DataLocked({ message }: { message: string }) {
  return (
    <Card className="border-[var(--color-p1)]/40">
      <div className="text-sm">
        <span className="text-[var(--color-p1)]">⏳ {message}</span>
        <p className="mt-1 text-[var(--color-muted)] text-xs">
          DuckDB 为单写锁，采集期间无法读取。采集结束后自动恢复。
        </p>
      </div>
    </Card>
  );
}

/**
 * 研报发布后 N 日的**超额**收益。
 *
 * 三种状态必须可区分——这是本组件存在的理由：
 *
 *   有数值      窗口已走完，超额收益（红涨绿跌）
 *   「未满 12/20」 窗口未走完，已走 12 个交易日
 *   「—」        无行情数据，或该月无基准可比
 *
 * 若窗口未满时留空，它与「数据缺了」在视觉上完全一样，
 * 而两者的含义相反：前者是「再等等」，后者是「这条不可信」。
 */
export function ReturnCell({
  value,
  horizon,
  elapsed,
}: {
  value: number | null;
  horizon: number;
  elapsed: number | null;
}) {
  if (value != null) {
    return (
      <span
        className="num"
        style={{ color: value > 0 ? "var(--color-up)" : "var(--color-down)" }}
      >
        {value > 0 ? "+" : ""}
        {(value * 100).toFixed(1)}%
      </span>
    );
  }
  if (elapsed != null && elapsed < horizon) {
    return (
      <span
        className="num text-[var(--color-muted)] text-xs"
        title={`发布后仅过去 ${elapsed} 个交易日，未满 ${horizon} 日，窗口尚未走完`}
      >
        未满 {elapsed}/{horizon}
      </span>
    );
  }
  return <span className="text-[var(--color-muted)]">—</span>;
}
