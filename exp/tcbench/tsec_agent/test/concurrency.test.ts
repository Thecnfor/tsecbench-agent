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
