import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** 百分比格式化。A 股红涨绿跌，符号由调用方按 value 正负决定。 */
export function pct(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

/** 金额按亿元显示 —— A 股财报量级下，元为单位无法阅读。 */
export function yi(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${(value / 1e8).toFixed(digits)} 亿`;
}

export function ratio(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) return "—";
  return value.toFixed(digits);
}

/** 涨跌配色。A 股红涨绿跌，与欧美市场相反。 */
export function trendClass(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value) || value === 0) return "text-[var(--color-muted)]";
  return value > 0 ? "text-[var(--color-up)]" : "text-[var(--color-down)]";
}
