import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 面板（React）构建：产物 dist/panel/，由 dist/panel-server.js 静态托管。
// dev：pnpm dev 起 vite dev server，/api 代理到本地面板服务（默认 8791）。
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist/panel",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8791",
    },
  },
});
