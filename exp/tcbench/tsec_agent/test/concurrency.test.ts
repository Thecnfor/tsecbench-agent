/** 真并发 Attempt turn：AGENT_BRANCH_CONCURRENCY>1 时两条 branch 的 LLM turn 重叠。 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { solveTask } from "../src/agent.js";
import { CFG } from "../src/config.js";
import { initRecords, resetRecords, setRecordsDir } from "../src/records.js";
import { setTaskCwd } from "../src/shell.js";
import type { ChatMessage, LlmClient } from "../src/types.js";

const SNAP = { ...CFG };

beforeEach(() => {
  Object.assign(CFG, SNAP);
  CFG.model = "deepseek-flash";
});
afterEach(() => {
  Object.assign(CFG, SNAP);
  resetRecords();
  setTaskCwd("");
});

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const tc = (id: string, name: string, args: Record<string, unknown>) => ({
  id,
  type: "function" as const,
  function: { name, arguments: JSON.stringify(args) },
});

describe("并发 Attempt turn", () => {
  it("concurrency=2：fork 后 trunk 与 branch 的 LLM 调用重叠", async () => {
    CFG.fork_enabled = true;
    CFG.planner_enabled = false;
    CFG.branch_concurrency = 2;
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "conc_rec_"));
    setRecordsDir(recdir);
    const R = initRecords("local", "deepseek-flash");
    const conv = R.conversation("t-conc");
    const workdir = fs.mkdtempSync(path.join(os.tmpdir(), "conc_work_"));
    setTaskCwd(workdir);

    let active = 0;
    let maxActive = 0;
    const client: LlmClient = {
      async create(opts) {
        active++;
        maxActive = Math.max(maxActive, active);
        try {
          await sleep(40);
          const msgs = opts.messages;
          const isBranch = msgs.some((m: ChatMessage) =>
            String(m.content ?? "").startsWith("【分支 1"),
          );
          if (isBranch) {
            const tools = msgs.filter((m) => m.role === "tool").length;
            if (tools === 0) {
              return {
                choices: [
                  {
                    message: {
                      role: "assistant",
                      content: null,
                      tool_calls: [tc("b1", "shell", { command: "echo branch1" })],
                    },
                  },
                ],
              };
            }
            return { choices: [{ message: { role: "assistant", content: "branch1 收束" } }] };
          }
          const tools = msgs.filter((m) => m.role === "tool").length;
          if (tools === 0) {
            return {
              choices: [
                {
                  message: {
                    role: "assistant",
                    content: null,
                    tool_calls: [tc("f1", "fork", { direction: "并行方向" })],
                  },
                },
              ],
            };
          }
          return { choices: [{ message: { role: "assistant", content: "trunk 收束" } }] };
        } finally {
          active--;
        }
      },
    };

    const r = await solveTask(client, "t-conc", "卡", 600, conv, { taskClass: "short" });
    expect(r.status).toBe("done");
    expect(maxActive).toBeGreaterThanOrEqual(2);
  }, 30_000);
});

describe("三分支并发公平性", () => {
  it("concurrency=2 时 trunk + 两个 fork 都获得 LLM turn，不饿死高 id 分支", async () => {
    CFG.fork_enabled = true;
    CFG.planner_enabled = false;
    CFG.branch_concurrency = 2;
    CFG.max_branches = 4;
    CFG.fork_cooldown_sec = 0;
    CFG.task_max_turns = 10;
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "conc3_rec_"));
    setRecordsDir(recdir);
    const R = initRecords("local", "deepseek-flash");
    const conv = R.conversation("t-conc3");
    setTaskCwd(fs.mkdtempSync(path.join(os.tmpdir(), "conc3_work_")));
    const calls = new Map<number, number>();
    const client: LlmClient = {
      async create(opts) {
        const msgs = opts.messages;
        const directive = msgs.find((m) => String(m.content ?? "").startsWith("【分支 "));
        const id = directive ? Number(/【分支 (\d+)/.exec(String(directive.content))?.[1]) : 0;
        calls.set(id, (calls.get(id) ?? 0) + 1);
        if (id === 0) {
          const forkCalls = msgs.filter((m) =>
            (m.tool_calls ?? []).some((t) => t.function.name === "fork"),
          ).length;
          if (forkCalls < 2) {
            return {
              choices: [
                {
                  message: {
                    role: "assistant",
                    content: null,
                    tool_calls: [tc(`f${forkCalls}`, "fork", { direction: `方向${forkCalls}` })],
                  },
                },
              ],
            };
          }
        }
        return {
          choices: [
            {
              message: {
                role: "assistant",
                content: null,
                tool_calls: [tc(`s${id}-${calls.get(id)}`, "shell", { command: "echo ok" })],
              },
            },
          ],
        };
      },
    };
    const r = await solveTask(client, "t-conc3", "卡", 600, conv, { taskClass: "short" });
    expect(r.status).toBe("paused");
    expect([...calls.keys()].sort()).toEqual([0, 1, 2]);
    expect(calls.get(0)).toBeGreaterThan(0);
    expect(calls.get(1)).toBeGreaterThan(0);
    expect(calls.get(2)).toBeGreaterThan(0);
  }, 30_000);
});
