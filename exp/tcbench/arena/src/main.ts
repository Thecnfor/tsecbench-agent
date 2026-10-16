/** 入口：配置 → 题包 → 驱动（cli|fake）→ 重启对账 → 镜像预热 → 监听。
 *
 * 用法：pnpm dev（或 ARENA_* 环境变量定制）。agent 侧接入只需把
 * BENCHMARK_BASE_URL 指向 http://<ARENA_ADVERTISE_HOST>:<port>，BENCHMARK_TOKEN 对上。
 */
import { loadCatalog } from "./catalog.js";
import { loadConfig } from "./config.js";
import { CliDriver } from "./docker.js";
import { FakeDriver } from "./fake.js";
import { buildServer, listen } from "./server.js";
import { ArenaState } from "./state.js";
import type { Driver } from "./types.js";

async function main(): Promise<void> {
  const cfg = loadConfig();
  const packs = loadCatalog(cfg.challengesDir);
  const driver: Driver = cfg.driver === "fake" ? new FakeDriver() : new CliDriver(cfg);
  const state = new ArenaState(packs, driver, cfg);
  await state.reconcile();
  if (driver.prebuild) {
    for (const spec of packs.values()) void driver.prebuild(spec); // 后台预热，不阻塞监听
  }
  const server = buildServer(state, cfg);
  const port = await listen(server, cfg);
  console.log(
    `[arena] listening on http://${cfg.bind}:${port}（advertise=${cfg.advertiseHost}, driver=${cfg.driver}, ` +
      `challenges=${packs.size}, maxActive=${cfg.maxActive}, ports=${cfg.portMin}-${cfg.portMax}, ` +
      `totalLimit=${cfg.totalLimitS || "∞"}s）`,
  );
  const shutdown = () => {
    console.log("[arena] shutting down");
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 3000).unref();
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}

main().catch((e: unknown) => {
  console.error(`[arena] 启动失败：${(e as Error).message}`);
  process.exit(1);
});
