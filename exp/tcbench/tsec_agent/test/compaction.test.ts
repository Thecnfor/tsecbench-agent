/** 共享前缀压缩：fork 断点选择 + 分支压缩锚重建 + 多 Attempt 复用。全离线。 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { selectCompactionScope, solveTask } from "../src/agent.js";
import { type BranchCfg, BranchManager } from "../src/branch.js";
import { CFG } from "../src/config.js";
import { initRecords, rebuildBranches, rec, resetRecords, setRecordsDir } from "../src/records.js";
import { setTaskCwd } from "../src/shell.js";
import type { ChatMessage, LlmClient } from "../src/types.js";

const SNAP = { ...CFG };
beforeEach(() => Object.assign(CFG, SNAP));
afterEach(() => {
  Object.assign(CFG, SNAP);
  resetRecords();
  setTaskCwd("");
});

const BCFG: BranchCfg = {
  enabled: true,
  maxBranches: 4,
  maxForks: 8,
  forkCooldownSec: 0,
  pruneSec: 600,
  plannerNudgeMax: 2,
  plannerNudgeCooldownSec: 400,
};

function baseMessages(n = 12): ChatMessage[] {
  const out: ChatMessage[] = [
    { role: "system", content: "sys" },
    { role: "user", content: "task card" },
  ];
  for (let i = 2; i < n; i++) {
    out.push({
      role: i % 2 === 0 ? "assistant" : "user",
      content: `prefix-${i}`,
    });
  }
  return out;
}

describe("共享前缀压缩边界", () => {
  it("优先选被最多 Attempt 共享的 fork checkpoint，而不是单个分支的完整上下文", () => {
    const base = baseMessages(12);
    const bm = new BranchManager(BCFG, base, 1000, "/work/tasks/x");
    bm.materializeFork(1, 0, "方向 A", 3, 1001, (id) => `/work/tasks/x/branches/${id}`);
    bm.materializeFork(2, 0, "方向 B", 4, 1002, (id) => `/work/tasks/x/branches/${id}`);
    const target = bm.get(1)!;
    const scope = selectCompactionScope(bm.alive(), target);
    expect(scope).not.toBeNull();
    expect(scope!.len).toBe(base.length); // 两个 fork 都从同一前缀分出
    expect(scope!.coverage).toBe(3); // trunk + b1 + b2
  });

  it("已有同前缀压缩摘要时覆盖数计入复用，支持后续 Attempt 直接复用", () => {
    const base = baseMessages(12);
    const bm = new BranchManager(BCFG, base, 1000, "/work/tasks/x");
    bm.materializeFork(1, 0, "方向 A", 3, 1001, (id) => `/work/tasks/x/branches/${id}`);
    bm.materializeFork(2, 0, "方向 B", 4, 1002, (id) => `/work/tasks/x/branches/${id}`);
    const target = bm.get(1)!;
    const first = selectCompactionScope(bm.alive(), target)!;
    const reused = selectCompactionScope(bm.alive(), target, new Set([first.key]))!;
    expect(reused.key).toBe(first.key);
    expect(reused.coverage).toBe(first.coverage + 1);
  });
});

describe("分支压缩锚重建", () => {
  it("压缩后的分支只保留 system/任务卡 + 摘要 + tail，不重复父前缀", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "compact_rebuild_"));
    const file = path.join(dir, "conversation.jsonl");
    const recs = [
      { ts: "t", seq: 1, code: "x", kind: "system", content: "sys", branch: 0 },
      { ts: "t", seq: 2, code: "x", kind: "user", content: "card", branch: 0 },
      { ts: "t", seq: 3, code: "x", kind: "assistant", output: "parent work", branch: 0 },
      {
        ts: "t",
        seq: 4,
        code: "x",
        kind: "user",
        branch: 1,
        kind_note: "fork_directive",
        parent_branch: 0,
        fork_seq: 3,
        fork_label: "方向 A",
        content: "【分支 1 · 方向】方向 A",
      },
      { ts: "t", seq: 5, code: "x", kind: "assistant", output: "before compact", branch: 1 },
      {
        ts: "t",
        seq: 6,
        code: "x",
        kind: "user",
        branch: 1,
        kind_note: "context_compact",
        content: "【系统·上下文压缩】summary",
      },
      { ts: "t", seq: 7, code: "x", kind: "assistant", output: "tail after compact", branch: 1 },
    ];
    fs.writeFileSync(file, recs.map((r) => JSON.stringify(r)).join("\n"));
    const branches = rebuildBranches(file);
    const b1 = branches.find((b) => b.id === 1)!;
    expect(b1.messages).toHaveLength(4); // system + card + summary + tail
    expect(String(b1.messages[2].content)).toContain("summary");
    expect(String(b1.messages[3].content)).toBe("tail after compact");
    expect(b1.messages.some((m) => String(m.content).includes("parent work"))).toBe(false);
  });
});

describe("多 Attempt 共享压缩", () => {
  it("400k 触发时先压共享前缀；第二个 Attempt 复用摘要且保留 tail", async () => {
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "compact_shared_"));
    setRecordsDir(recdir);
    const R = initRecords("local", "deepseek-flash");
    const conv = R.conversation("t-compact-shared");
    const workdir = fs.mkdtempSync(path.join(os.tmpdir(), "compact_work_"));
    setTaskCwd(workdir);
    CFG.fork_enabled = true;
    CFG.planner_enabled = false;
    CFG.branch_concurrency = 1;
    CFG.compact_tokens = 300_000; // 测试压缩路径，不造 400k 真实消息
    CFG.task_max_turns = 10;
    let n = 0;
    let compactCalls = 0;
    const tc = (id: string, name: string, args: Record<string, unknown>) => ({
      id,
      type: "function" as const,
      function: { name, arguments: JSON.stringify(args) },
    });
    const client = {
      async create(opts: { messages: ChatMessage[] }) {
        const last = String(opts.messages.at(-1)?.content ?? "");
        if (last.includes("即将被压缩为一份")) {
          compactCalls++;
          return { choices: [{ message: { role: "assistant", content: `DOC-${compactCalls}` } }] };
        }
        const hasCompact = opts.messages.some((m) =>
          String(m.content ?? "").includes("【系统·上下文压缩】"),
        );
        const hasDirective = opts.messages.some((m) =>
          String(m.content ?? "").startsWith("【分支"),
        );
        const hasForkCall = opts.messages.some((m) =>
          (m.tool_calls ?? []).some((t) => t.function.name === "fork"),
        );
        n++;
        if (!hasForkCall && !hasDirective && !hasCompact) {
          return {
            choices: [
              {
                message: {
                  role: "assistant",
                  content: null,
                  tool_calls: [tc("f1", "fork", { direction: "共享前缀分支" })],
                },
              },
            ],
            usage: { prompt_tokens: n <= 2 ? 500_000 : 1000 },
          };
        }
        return {
          choices: [
            {
              message: {
                role: "assistant",
                content: null,
                tool_calls: [tc(`s${n}`, "shell", { command: "echo ok", wait_ms: 10 })],
              },
            },
          ],
          usage: { prompt_tokens: n <= 2 ? 500_000 : 1000 },
        };
      },
    } as LlmClient;
    const r = await solveTask(client, "t-compact-shared", "任务卡", 600, conv, {
      flagTotal: 2,
      startTurn: 0,
    });
    expect(r.turnsUsed).toBeLessThanOrEqual(10);
    const events = fs
      .readFileSync(path.join(recdir, "events.jsonl"), "utf8")
      .trim()
      .split("\n")
      .filter(Boolean)
      .map((l) => JSON.parse(l) as Record<string, unknown>)
      .filter((e) => e.type === "context_compacted");
    expect(events).toHaveLength(2);
    expect(events.some((e) => e.reused === true)).toBe(true);
    expect(compactCalls).toBe(1); // 第二个 Attempt 复用共享摘要，不再调压缩 LLM

    const convRecs = fs
      .readFileSync(path.join(recdir, "tasks", "t-compact-shared", "conversation.jsonl"), "utf8")
      .trim()
      .split("\n")
      .filter(Boolean)
      .map((l) => JSON.parse(l) as Record<string, unknown>);
    expect(convRecs.filter((x) => x.kind_note === "context_compact")).toHaveLength(2);
    // 续跑重建：trunk / 分支都从锚 + compact_tail 还原，摘要不重复父前缀。
    expect(
      R.loadMessages("t-compact-shared").some((m) => String(m.content ?? "").includes("DOC-1")),
    ).toBe(true);
    const loadedB1 = R.loadBranches("t-compact-shared").find((b) => b.id === 1)!;
    expect(loadedB1.messages.some((m) => String(m.content ?? "").includes("DOC-1"))).toBe(true);
    const b1 = r.branches.find((b) => b.id === 1)!;
    expect(String(r.messages[2]?.content ?? "")).toContain("【系统·上下文压缩】");
    expect(String(b1.messages[2]?.content ?? "")).toContain("【系统·上下文压缩】");
    expect(b1.messages.some((m) => String(m.content ?? "").startsWith("【分支"))).toBe(true);
  }, 120_000);
});

describe("planner 覆盖检查接线", () => {
  it("多 flag 开局只有一条 active 线时注入覆盖提醒", async () => {
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "coverage_nudge_"));
    setRecordsDir(recdir);
    const R = initRecords("local", "deepseek-flash");
    const conv = R.conversation("t-coverage");
    setTaskCwd(fs.mkdtempSync(path.join(os.tmpdir(), "coverage_work_")));
    CFG.fork_enabled = true;
    CFG.planner_enabled = true;
    CFG.task_max_turns = 1;
    let saw = false;
    const client = {
      async create(opts: { messages: ChatMessage[] }) {
        saw = opts.messages.some((m) =>
          String(m.content ?? "").includes("[planner 提醒·覆盖检查]"),
        );
        return {
          choices: [
            {
              message: {
                role: "assistant",
                content: null,
                tool_calls: [
                  {
                    id: "coverage-1",
                    type: "function" as const,
                    function: { name: "shell", arguments: '{"command":"echo ok","wait_ms":10}' },
                  },
                ],
              },
            },
          ],
        };
      },
    } as LlmClient;
    const r = await solveTask(client, "t-coverage", "任务卡", 600, conv, { flagTotal: 2 });
    expect(saw).toBe(true);
    expect(
      r.messages.some((m) => String(m.content ?? "").includes("[planner 提醒·覆盖检查]")),
    ).toBe(true);
    expect(r.status).toBe("paused");
  }, 120_000);
});
