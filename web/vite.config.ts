import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(import.meta.dirname, "./src") },
  },
  server: {
    port: 5173,
    proxy: {
      // 后端复用 csg 已有的 PIT 查询与指标计算。
      // 金融逻辑只实现一次——disclosure_date 过滤那类要害绝不能有第二份实现。
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        // 图表库体积远大于业务代码，单独分包避免每次改业务都让用户重下
        manualChunks(id: string) {
          if (/echarts|lightweight-charts/.test(id)) return "charts";
          if (/node_modules\/(react|react-dom|react-router)/.test(id)) return "vendor";
        },
      },
    },
  },
});
