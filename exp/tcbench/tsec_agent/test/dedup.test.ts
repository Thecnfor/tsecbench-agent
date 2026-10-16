/** 调度去重回归测试（arena 事故，见 ../docker/runs/arena-dev-token/BUGREPORT）：
 * `running` 是 Map，去重曾误用 `in`（检查属性，对 Map 恒 false）→ eligible 永远放行
 * → 同题重复派发（arena 实测 4 分钟 110 个并发 worker 写同一 conversation.jsonl，
 * system 重置 109 次、多写者交错落盘、resume 重建混合文件 → DeepSeek 400）。
 * 本测试让 worker 阻塞在 LLM 调用期间多转几轮调度，断言 start 间隔不小于
 * worker 生命周期（即阻塞期间绝不二次派发）。 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it, vi } from "vitest";
import { CFG } from "../src/config.js";
import type { LlmClient } from "../src/types.js";

const fake = vi.hoisted(() => {
  const state = {
    starts: [] as number[], // 每次 start 的 epoch 秒（断言核心）
  };
  return { state };
});

vi.mock("../src/bench.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/bench.js")>();
  let fetchN = 0;
  return {
    ...actual,
    fetchChallenges: async () => {
      fetchN++;
      // 第 6 次快照触发平台终态（评测截止）→ STOP 收束调度循环。
      // 只抛一次：worker 段后校验的重试快照仍要正常返回（否则收尾拖 12s）。
      if (fetchN === 6) throw new actual.TaskFinishedError("finished", "already finished", 409);
      return [
        {
          unique_code: "dup-01",
          description: "去重测试题",
          difficulty: "easy",
          level: 1,
          total_score: 100,
          flag_count: 1,
          correct_flag_count: 0,
          is_completed: false,
          container_status: "stopped",
          container_addr: [],
        },
      ];
    },
    startChallenge: async (code: string) => {
      fake.state.starts.push(Date.now() / 1000);
      return {
        unique_code: code,
        container_status: "available",
        container_addr: ["10.9.9.9:80"],
      };
    },
    closeChallenge: async () => ({}),
    releaseCompleted: async () => [] as string[],
  };
});

describe("调度去重（Map 必须用 has；in 对 Map 恒 false 的历史 bug）", () => {
  it("worker 阻塞期间调度循环不重复派发同一题", async () => {
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "dedup_"));
    process.env.RECORDS_DIR = recdir;
    process.env.AGENT_MODE = "local";
    process.env.AGENT_TASKS = "";
    process.env.AGENT_TASK_FILTER = "dup-01";
    process.env.AGENT_PLATFORM_WATCH = "0";
    process.env.VPN_GUARD = "0";
    process.env.AGENT_DEADLINE_SEC = "0"; // 不限：时长由平台终态控制（deadline<=60 会即刻 break）
    process.env.AGENT_MAX_PARALLEL = "3";
    // config 模块的 CFG 在 import 时已按环境变量取值——直接改 CFG 兜底（同 e2e 手法）
    CFG.deadline_sec = 0;
    CFG.tasks = "";
    CFG.model = "deepseek-flash";

    const { run } = await import("../src/agent.js");
    const { resetRecords, initRecords } = await import("../src/records.js");
    resetRecords();
    initRecords("local", "deepseek-flash");

    const PENDING_SEC = 6; // worker 生命周期（每次 LLM 调用阻塞 6s）
    const client: LlmClient = {
      create: () =>
        new Promise((resolve) => {
          setTimeout(
            () => resolve({ choices: [{ message: { role: "assistant", content: "段总结" } }] }),
            PENDING_SEC * 1000,
          );
        }),
    };

    await run(client);
    const starts = fake.state.starts;

    expect(starts.length, "至少派发过一次").toBeGreaterThanOrEqual(1);
    // 核心断言：任意相邻两次 start 间隔 ≥ worker 生命周期（-1s 容差）。
    // 修复前：调度每 ~2-4s 一轮，阻塞期间反复 start → 间隔远小于 6s。
    for (let i = 1; i < starts.length; i++) {
      const gap = starts[i] - starts[i - 1];
      expect(
        gap,
        `第 ${i + 1} 次 start 距第 ${i} 次仅 ${gap.toFixed(1)}s —— 上一 worker 未死就重复派发`,
      ).toBeGreaterThanOrEqual(PENDING_SEC - 1);
    }
    // 14s deadline / 6s worker 生命周期 → 最多 2 次（首派 + 一次合法续段）；
    // 修复前阻塞期每轮都 start（4-6 次）。
    expect(starts.length).toBeLessThanOrEqual(2);
  }, 60_000);
});
