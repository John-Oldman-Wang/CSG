import { NavLink, Route, Routes } from "react-router-dom";
import { useTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";
import Dashboard from "@/pages/Dashboard";
import InstitutionStats from "@/pages/InstitutionStats";
import PoolTiers from "@/pages/PoolTiers";
import ReportAnalysis from "@/pages/ReportAnalysis";
import Reports from "@/pages/Reports";
import StockDetail from "@/pages/StockDetail";
import TaskDetail from "@/pages/TaskDetail";
import Tasks from "@/pages/Tasks";
import Validations from "@/pages/Validations";

const NAV = [
  { to: "/", label: "总览", end: true },
  { to: "/pool", label: "股票池" },
  { to: "/tasks", label: "复核任务" },
  { to: "/reports", label: "研报检索" },
  { to: "/institutions", label: "机构胜率" },
  { to: "/validations", label: "验证结果" },
];

export default function App() {
  const { theme, toggle } = useTheme();

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-6 border-[var(--color-border)] border-b px-5 py-3">
        <span className="font-semibold text-sm tracking-wide">CSG</span>
        <nav className="flex gap-1">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) =>
                cn(
                  "rounded px-3 py-1.5 text-sm transition-colors",
                  isActive
                    ? "bg-[var(--color-surface)] text-[var(--color-fg)]"
                    : "text-[var(--color-muted)] hover:text-[var(--color-fg)]",
                )
              }
            >
              {n.label}
            </NavLink>
          ))}
        </nav>
        <span className="ml-auto text-[var(--color-muted)] text-xs">
          判断归你，系统只整理事实
        </span>
        <button
          type="button"
          onClick={toggle}
          title={theme === "dark" ? "切换到亮色" : "切换到暗色"}
          className="rounded border border-[var(--color-border)] px-2 py-1 text-sm transition-colors hover:bg-[var(--color-surface)]"
        >
          {theme === "dark" ? "☀" : "☾"}
        </button>
      </header>

      <main className="flex-1 overflow-auto p-5">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/pool" element={<PoolTiers />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/tasks/:taskId" element={<TaskDetail />} />
          <Route path="/stocks/:code" element={<StockDetail />} />
          <Route path="/reports" element={<Reports />} />
          <Route path="/reports/:reportId" element={<ReportAnalysis />} />
          <Route path="/institutions" element={<InstitutionStats />} />
          <Route path="/validations" element={<Validations />} />
        </Routes>
      </main>
    </div>
  );
}
