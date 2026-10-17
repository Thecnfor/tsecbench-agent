/** 同轮多 tool_call 并发派发（方案 A）：一轮里多条 shell 同时起跑（墙钟≈最慢一条，
 * 不再串行排队）；结果仍按 tool_calls 原序收割与配对（br.messages / conversation
 * 顺序与串行语义一致）。耗时断言用「两条 sleep 1 并发 ≈ 1 条的墙钟」这个 2 倍差距，
 * 阈值放宽到 1.7s 防慢机抖动。 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { solveTask } from "../src/agent.js";
import { CFG } from "../src/config.js";
import { initRecords, rec, resetRecords, setRecordsDir } from "../src/records.js";
import { setTaskCwd } from "../src/shell.js";
import type { LlmClient, ToolCall } from "../src/types.js";

const SNAP = { ...CFG };
let recdir = "";
let workdir = "";

beforeEach(() => {
  Object.assign(CFG, SNAP);
  CFG.model = "deepseek-flash";
  recdir = fs.mkdtempSync(path.join(os.tmpdir(), "par_"));
  setRecordsDir(recdir);
  resetRecords();
  initRecords("local", "deepseek-flash");
  workdir = fs.mkdtempSync(path.join(os.tmpdir(), "par_work_"));
  setTaskCwd(workdir);
});
afterEach(() => {
  Object.assign(CFG, SNAP);
  resetRecords();
  setTaskCwd("");
});

function tc(id: string, name: string, args: Record<string, unknown>) {
  return { id, type: "function" as const, function: { name, arguments: JSON.stringify(args) } };
}

function clientWithTurns(
  turns: Array<() => { content?: string | null; tool_calls?: ToolCall[] }>,
): LlmClient {
  let i = 0;
  return {
    async create() {
      const r = turns[Math.min(i, turns.length - 1)];
      i++;
      return { choices: [{ message: { role: "assistant", ...r() } }] };
    },
  };
}

describe("同轮多 tool_call 并发派发", () => {
  it("两条 sleep 1 并发 ≈ 1 条墙钟；结果按 tool_calls 原序配对", async () => {
    const conv = rec().conversation("t-par1");
    const client = clientWithTurns([
      () => ({
        content: null,
        tool_calls: [
          tc("p1", "shell", { command: "sleep 1; echo FIRST", label: "slow-a", wait_sec: 5 }),
          tc("p2", "shell", { command: "sleep 1; echo SECOND", label: "slow-b", wait_sec: 5 }),
        ],
      }),
      () => ({ content: "两条都收完了。" }),
    ]);
    const t0 = Date.now();
    const r = await solveTask(client, "t-par1", "卡", 600, conv, {});
    const elapsed = Date.now() - t0;
    expect(r.status).toBe("done");
    // 串行需要 ≥2s（两条 sleep 1 排队）；并发 ≈1.1s。阈值 1.7s 卡在两者之间，留慢机余量
    expect(elapsed).toBeLessThan(1700);

    // 配对与顺序：assistant 的 tool_calls 顺序 = tool 消息顺序，且内容分别对应
    const msgs = r.messages;
    const toolMsgs = msgs.filter((m) => m.role === "tool");
    expect(toolMsgs.length).toBe(2);
    expect(toolMsgs[0].tool_call_id).toBe("p1");
    expect(toolMsgs[1].tool_call_id).toBe("p2");
    expect(String(toolMsgs[0].content)).toContain("FIRST");
    expect(String(toolMsgs[1].content)).toContain("SECOND");

    // 记录顺序同样按 tool_calls 原序落盘
    const recs = fs
      .readFileSync(path.join(recdir, "tasks", "t-par1", "conversation.jsonl"), "utf8")
      .split("\n")
      .filter(Boolean)
      .map((l) => JSON.parse(l) as Record<string, unknown>)
      .filter((x) => x.kind === "tool");
    expect(recs.map((x) => x.tool_call_id)).toEqual(["p1", "p2"]);
  }, 30_000);

  it("shell+submit_flag 混合同轮：都并发派发，记录齐全", async () => {
    const conv = rec().conversation("t-par2");
    // submit 走真实 bench.fetch → stub 掉
    const origFetch = globalThis.fetch;
    globalThis.fetch = (async () =>
      new Response(
        JSON.stringify({
          correct: true,
          matched_flag_index: 0,
          correct_flag_count: 1,
          total_flag_count: 1,
        }),
        { status: 200 },
      )) as typeof fetch;
    CFG.benchmark_token = "t";
    CFG.benchmark_base_url = "http://bench.test";
    try {
      const client = clientWithTurns([
        () => ({
          content: null,
          tool_calls: [
            tc("s1", "shell", { command: "sleep 0.5; echo work", wait_sec: 5 }),
            tc("f1", "submit_flag", { flag: "flag{par}" }),
          ],
        }),
        () => ({ content: "提交完成。" }),
      ]);
      const r = await solveTask(client, "t-par2", "卡", 600, conv, { flagTotal: 1 });
      expect(r.status).toBe("done");
      const toolMsgs = r.messages.filter((m) => m.role === "tool");
      expect(toolMsgs.map((m) => m.tool_call_id)).toEqual(["s1", "f1"]);
      expect(String(toolMsgs[0].content)).toContain("work");
      expect(String(toolMsgs[1].content)).toContain("correct");
    } finally {
      globalThis.fetch = origFetch;
    }
  }, 30_000);
});
