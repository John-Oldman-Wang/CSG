import { NavLink, Route, Routes } from "react-router-dom";
import { cn } from "@/lib/utils";
import Dashboard from "@/pages/Dashboard";
import StockDetail from "@/pages/StockDetail";
import TaskDetail from "@/pages/TaskDetail";
import Tasks from "@/pages/Tasks";
import Validations from "@/pages/Validations";

const NAV = [
  { to: "/", label: "总览", end: true },
  { to: "/tasks", label: "复核任务" },
  { to: "/validations", label: "验证结果" },
];

export default function App() {
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
      </header>

      <main className="flex-1 overflow-auto p-5">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/tasks/:taskId" element={<TaskDetail />} />
          <Route path="/stocks/:code" element={<StockDetail />} />
          <Route path="/validations" element={<Validations />} />
        </Routes>
      </main>
    </div>
  );
}
