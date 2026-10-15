import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    setupFiles: ["test/setup.ts"],
    environment: "node",
    testTimeout: 120_000,
    hookTimeout: 60_000,
    // 自测（e2e）会跑完整调度循环，单文件串行执行避免互相干扰
    fileParallelism: false,
  },
});
