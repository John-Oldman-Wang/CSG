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
