import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { DataLockedError } from "./api/client";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      // 数据库被采集任务锁定是**已知状态**而非故障，重试无意义——
      // 采集可能持续数十分钟，重试只会刷屏。交由界面提示用户稍后再来。
      retry: (failureCount, error) => !(error instanceof DataLockedError) && failureCount < 2,
      refetchOnWindowFocus: false,
    },
  },
});

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
