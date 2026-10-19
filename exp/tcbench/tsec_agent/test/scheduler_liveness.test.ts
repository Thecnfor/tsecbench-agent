/** 调度活性回归（离线）：
 *
 * 复现“所有其他题都暂停后，当前题一收尾就 close→start”的抖动：
 * - 并发 1 槽、2 道题；
 * - 每道题第一次只输出无工具总结（模拟模型提前收尾 / 错提交后误以为结束）；
 * - 第二次提交正确 flag，第三次输出收尾总结。
 *
 * 期望：暂停中的题不应被算作“fresh 等槽者”。最后一题应在原容器/原上下文继续，
 * 不能因为另一道暂停题而 close→start；它只 start 一次。
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";

const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "tsec_sched_live_"));
process.env.AGENT_MODE = "local";
process.env.RECORDS_DIR = recdir;
process.env.AGENT_PLATFORM_WATCH = "0";
process.env.VPN_GUARD = "0";
process.env.AGENT_MAX_PARALLEL = "1";
process.env.AGENT_PLATFORM_CAPACITY = "1";
process.env.AGENT_TASK_MAX_TURNS = "10";
process.env.AGENT_GIVEUP_MAX = "3";
process.env.AGENT_NO_WAITER_GIVEUP_MAX = "0";
process.env.AGENT_FLAG_VERIFY_DELAY_SEC = "0";
process.env.AGENT_FLAG_VERIFY_TIMEOUT_SEC = "1";
process.env.AGENT_TASK_FILTER = "";

const fake = vi.hoisted(() => {
  type Chal = {
    unique_code: string;
    description: string;
    difficulty: string;
    level: number;
    total_score: number;
    flag_count: number;
    correct_flag_count: number;
    is_completed: boolean;
    container_status: string;
    container_addr: string[];
  };
  const make = (code: string): Chal => ({
    unique_code: code,
    description: "liveness test",
    difficulty: "easy",
    level: 1,
    total_score: 100,
    flag_count: 1,
    correct_flag_count: 0,
    is_completed: false,
    container_status: "stopped",
    container_addr: [],
  });
  return {
    chals: [make("t-a"), make("t-b")],
    starts: [] as string[],
    closes: [] as string[],
    reset() {
      for (const c of this.chals) {
        c.correct_flag_count = 0;
        c.is_completed = false;
        c.container_status = "stopped";
        c.container_addr = [];
      }
      this.starts.length = 0;
      this.closes.length = 0;
    },
  };
});

vi.mock("../src/bench.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/bench.js")>();
  const findByCode = (code: string) => fake.chals.find((c) => c.unique_code === code);
  return {
    ...actual,
    fetchChallenges: async () => fake.chals.map((c) => ({ ...c })),
    startChallenge: async (code: string) => {
      fake.starts.push(code);
      const c = findByCode(code);
      if (c) {
        c.container_status = "available";
        c.container_addr = [`10.9.9.${fake.starts.length}:80`];
      }
      return { unique_code: code, container_addr: c?.container_addr ?? [] };
    },
    closeChallenge: async (code: string) => {
      fake.closes.push(code);
      const c = findByCode(code);
      if (c) c.container_status = "stopped";
      return { unique_code: code, closed: true };
    },
    submitFlag: async (code: string, flag: string) => {
      const c = findByCode(code);
      if (flag.includes("wrong")) {
        return { correct: false, error: "invalid flag" };
      }
      if (c) {
        c.correct_flag_count = 1;
        c.is_completed = true;
      }
      return {
        correct: true,
        matched_flag_index: 0,
        correct_flag_count: 1,
        total_flag_count: 1,
      };
    },
    releaseCompleted: async (items: Array<{ unique_code: string; is_completed: boolean }>) => {
      const closed: string[] = [];
      for (const c of items) {
        const row = findByCode(c.unique_code);
        if (c.is_completed && row?.container_status === "available") {
          fake.closes.push(c.unique_code);
          row.container_status = "stopped";
          closed.push(c.unique_code);
        }
      }
      return closed;
    },
  };
});

function makeFakeClient(wrongFirst = false) {
  const calls = new Map<string, number>();
  return {
    async create(opts: { tools?: unknown; messages: Array<{ role: string; content?: unknown }> }) {
      // 通关复盘：无 tools，直接返回总结。
      if (opts.tools === undefined) {
        return { choices: [{ message: { role: "assistant", content: "复盘完成" } }] };
      }
      const firstUser = opts.messages.find((m) => m.role === "user");
      const m = /unique_code:\s*([a-z0-9-]+)/i.exec(String(firstUser?.content ?? ""));
      const code = m?.[1] ?? "unknown";
      const n = (calls.get(code) ?? 0) + 1;
      calls.set(code, n);
      const submit = (flag: string) => ({
        choices: [
          {
            message: {
              role: "assistant",
              content: null,
              tool_calls: [
                {
                  id: `submit-${code}`,
                  type: "function",
                  function: { name: "submit_flag", arguments: JSON.stringify({ flag }) },
                },
              ],
            },
          },
        ],
      });
      // 错提交场景：第一次错提交 → 第二次提前收尾（错提交后模型误以为结束）→ 第三次正确。
      // 第二次虽然模型收尾且有 fresh 新题等槽，controller 也不能 close。
      if (wrongFirst && code === "t-a") {
        if (n === 1) return submit("flag{wrong}");
        if (n === 2) {
          return {
            choices: [{ message: { role: "assistant", content: "先停一下。" } }],
          };
        }
        if (n === 3) return submit(`flag{${code}}`);
      }
      if (n === 1) {
        // 第一次提前收尾：本条应触发 giveup_continue，而不是 close→start。
        return {
          choices: [{ message: { role: "assistant", content: "这一轮先停一下。" } }],
        };
      }
      if (n === 2) {
        return submit(`flag{${code}}`);
      }
      return {
        choices: [{ message: { role: "assistant", content: "本题 flag 已提交，收尾。" } }],
      };
    },
  };
}

function readJsonl(file: string): any[] {
  if (!fs.existsSync(file)) return [];
  return fs
    .readFileSync(file, "utf8")
    .split("\n")
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l));
}

describe("调度活性：暂停题不触发抖动 close", () => {
  beforeEach(() => fake.reset());

  it("2 题/1 槽：最后一题不因另一道暂停题抖动重启", async () => {
    const { resolveAsync } = await import("../src/config.js");
    await resolveAsync();
    const agent = await import("../src/agent.js");
    const client = makeFakeClient();

    const rc = await agent.run(client as any);
    expect(rc).toBe(0);

    expect(fake.starts.filter((c) => c === "t-b")).toHaveLength(1);
    expect(fake.closes.filter((c) => c === "t-b")).toHaveLength(1);

    const evs = readJsonl(path.join(recdir, "events.jsonl"));
    const types = evs.map((e) => e.type);
    expect(types).toContain("giveup_continue");
    expect(types).not.toContain("task_abandoned");
    const starts = readJsonl(path.join(recdir, "tasks.jsonl")).filter(
      (r) => r.phase === "task_start",
    );
    expect(starts.filter((r) => r.code === "t-b")).toHaveLength(1);
  }, 30_000);

  it("无进展熔断：冷却中的暂停题排到所有候选之后，但仍可被调度", async () => {
    const { orderCandidates } = await import("../src/agent.js");
    const paused = [
      { ...fake.chals[0], unique_code: "stuck", correct_flag_count: 0 } as any,
      { ...fake.chals[1], unique_code: "progress", correct_flag_count: 1 } as any,
    ];
    const fresh = [{ ...fake.chals[0], unique_code: "fresh" } as any];
    const ordered = orderCandidates({
      freshLong: [],
      freshShort: fresh,
      paused,
      cap: 1,
      agingSec: 600,
      nowSec: 0,
      waitAgeOf: () => 0,
      runningLong: 0,
      cooled: new Set(["stuck"]),
    }).map((c) => c.unique_code);
    // 冷却题仍然在候选里（不判死），但排在有进展暂停题和 fresh 之后。
    expect(ordered).toContain("stuck");
    expect(ordered[ordered.length - 1]).toBe("stuck");
    expect(ordered.indexOf("progress")).toBeLessThan(ordered.indexOf("stuck"));
  });

  it("错提交后提前收尾：即使有 fresh 新题等槽也不 close", async () => {
    const { resolveAsync } = await import("../src/config.js");
    await resolveAsync();
    const agent = await import("../src/agent.js");
    const client = makeFakeClient(true);

    const rc = await agent.run(client as any);
    expect(rc).toBe(0);
    expect(fake.starts.filter((c) => c === "t-a")).toHaveLength(1);
    expect(fake.starts.filter((c) => c === "t-b")).toHaveLength(1);
  }, 30_000);
});
