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
