import { resolve } from "node:path";
import { defineConfig } from "vite";

// Node 侧构建（agent CLI + 面板服务）：ESM 产物，运行时零第三方依赖（镜像不带 node_modules）。
// 产物：dist/agent.js、dist/panel-server.js。
// 注意 emptyOutDir=false：先跑 build:panel 产出 dist/panel/，再跑本配置不能把它清掉
//（vite 默认会清 outDir，曾因此把面板产物清掉导致 "/" 404）。
export default defineConfig({
  build: {
    outDir: "dist",
    emptyOutDir: false,
    target: "node20",
    sourcemap: false,
    ssr: true,
    rollupOptions: {
      input: {
        agent: resolve(import.meta.dirname, "src/agent.ts"),
        "panel-server": resolve(import.meta.dirname, "src/panel/server.ts"),
      },
      output: {
        entryFileNames: "[name].js",
        format: "esm",
      },
    },
  },
});
