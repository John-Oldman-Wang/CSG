import { useEffect, useState } from "react";

export type Theme = "dark" | "light";

const KEY = "csg-theme";

/** 读取初始主题：优先用户选择，其次跟随系统。
 *  默认暗色——投研界面数据密度高，长时间盯盘更省眼。 */
function initial(): Theme {
  const saved = localStorage.getItem(KEY);
  if (saved === "dark" || saved === "light") return saved;
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(initial);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(KEY, theme);
  }, [theme]);

  return {
    theme,
    toggle: () => setTheme((t) => (t === "dark" ? "light" : "dark")),
  };
}
