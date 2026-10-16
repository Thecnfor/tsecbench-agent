/** 对话完整性套件：常规/fork/旁观者三模式下「无无用重复对话信息」。
 *
 * 覆盖维度（arena 事故后补强）：
 * 1. 常规：首段恰一次 system+task card；续段（messages 传递）不重写；真失败重启
 *    的多 system 是有意设计（rebuild 从最后一次 system 起）且 rebuild 只取末段；
 * 2. fork：fork_directive/core_log/broadcast 注入/flag 各恰一条；续段（messages+branches
 *    传递）不新增；rebuild（trunk+分支）与内存 messages 无双份；
 * 3. 旁观者：插嘴在 messages 与 conv 各恰一条；续段不重复；冷却限频不二次注入；
 * 4. 通用：conv 中 (kind, branch, content) 三元组无重复（note/error 语义性重复除外）。 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { solveTask } from "../src/agent.js";
import { BYSTANDER_SYSTEM } from "../src/bystander.js";
import { CFG } from "../src/config.js";
import { readCorelog } from "../src/corelog.js";
import { initRecords, rec, resetRecords, setRecordsDir } from "../src/records.js";
import { setTaskCwd } from "../src/shell.js";
import type { ChatMessage, LlmClient } from "../src/types.js";

const SNAP = { ...CFG };
let recdir = "";
let workdir = "";

beforeEach(() => {
  Object.assign(CFG, SNAP);
  CFG.model = "deepseek-flash";
  recdir = fs.mkdtempSync(path.join(os.tmpdir(), "integrity_"));
  setRecordsDir(recdir);
  initRecords("local", "deepseek-flash");
  workdir = fs.mkdtempSync(path.join(os.tmpdir(), "integrity_work_"));
  setTaskCwd(workdir);
});
afterEach(() => {
  Object.assign(CFG, SNAP);
  resetRecords();
  setTaskCwd("");
});

// ------------------------------------------------------------------ helpers --
function tc(id: string, name: string, args: Record<string, unknown>) {
  return { id, type: "function" as const, function: { name, arguments: JSON.stringify(args) } };
}

/** 按 (分支, 本分支已有工具轮数) 状态机派发的脚本 client。 */
function makeClient(
  script: Array<{
    when: (m: ChatMessage[], branch: string, ownTools: number, bySeen: boolean) => boolean;
    respond: () => {
      content?: string | null;
      tool_calls?: Array<{
        id: string;
        type: "function";
        function: { name: string; arguments: string };
      }>;
    };
  }>,
) {
  let seq = 0;
  const client: LlmClient = {
    async create(opts) {
      const msgs = opts.messages;
      const sys = String(msgs.find((m) => m.role === "system")?.content ?? "");
      if (sys === BYSTANDER_SYSTEM) {
        return {
          choices: [{ message: { role: "assistant", content: "换抽象层审计 JS 源码。" } }],
        };
      }
      const dir = msgs.find((m) => String(m.content ?? "").startsWith("【分支"));
      const branch = dir ? `b${/【分支 (\d+)/.exec(String(dir.content))?.[1]}` : "trunk";
      const from = dir ? msgs.indexOf(dir) : 0;
      const ownTools = msgs.slice(from).filter((m) => m.role === "tool").length;
      const bySeen = msgs.some((m) => String(m.content ?? "").startsWith("[旁观者提示]"));
      for (const s of script) {
        if (s.when(msgs, branch, ownTools, bySeen)) {
          const r = s.respond();
          return { choices: [{ message: { role: "assistant", ...r } }] };
        }
      }
      throw new Error(`integrity fake: no script matched (call ${++seq}, ${branch})`);
    },
  };
  return client;
}

function convOf(code: string): Array<Record<string, unknown>> {
  return fs
    .readFileSync(path.join(recdir, "tasks", code, "conversation.jsonl"), "utf8")
    .split("\n")
    .filter(Boolean)
    .map((l) => JSON.parse(l) as Record<string, unknown>);
}
const count = (
  recs: Array<Record<string, unknown>>,
  pred: (r: Record<string, unknown>) => boolean,
) => recs.filter(pred).length;

/** 通用去重：system/user/tool 的 (kind, branch, content) 三元组在 conv 中唯一
 *（assistant 自然语言 output 允许重复——模型两次说"好的"合法；tool 以 id 唯一）。 */
function assertNoDuplicateContent(recs: Array<Record<string, unknown>>): void {
  const seen = new Set<string>();
  for (const r of recs) {
    if (r.kind === "note" || r.kind === "error" || r.kind === "assistant") continue;
    const key = `${r.kind}|${r.branch ?? 0}|${String(r.content ?? r.result ?? r.tool_call_id ?? "")}`;
    expect(seen.has(key), `conv 出现重复对话信息: ${key.slice(0, 120)}`).toBe(false);
    seen.add(key);
  }
}

const FLAG_JSON =
  '{"correct":true,"matched_flag_index":1,"correct_flag_count":1,"total_flag_count":1}';

// -------------------------------------------------------------------- tests --
describe("常规模式完整性", () => {
  it("首段恰一次 system+task card；续段 messages 传递不重写", async () => {
    const conv = rec().conversation("t-i1");
    const client = makeClient([
      {
        when: () => true,
        respond: () => ({
          content: null,
          tool_calls: [tc("s1", "shell", { command: "echo probe" })],
        }),
      },
      { when: () => true, respond: () => ({ content: "段内继续" }) },
    ]);
    // 段1：2 轮后被轮数安全网收段（paused）
    CFG.task_max_turns = 2;
    const r1 = await solveTask(client, "t-i1", "任务卡", 600, conv, { taskClass: "short" });
    expect(r1.status).toBe("paused");

    // 段2：messages 传递续跑 → 不得重写 system/task card
    CFG.task_max_turns = 3;
    const r2 = await solveTask(client, "t-i1", "任务卡", 600, conv, {
      messages: r1.messages,
      startTurn: r1.turnsUsed,
      taskClass: "short",
    });
    expect(r2.status).toBe("paused");

    const recs = convOf("t-i1");
    expect(count(recs, (r) => r.kind === "system")).toBe(1);
    expect(count(recs, (r) => r.kind === "user" && r.content === "任务卡")).toBe(1);
    // 无 fork/旁观者时不应出现任何 branch>0 或特殊注入记录
    expect(count(recs, (r) => (r.branch ?? 0) !== 0)).toBe(0);
    expect(count(recs, (r) => r.kind_note !== undefined)).toBe(0);
    assertNoDuplicateContent(recs);
  });

  it("真失败重启的第二条 system 是有意设计：rebuild 只取末段上下文", async () => {
    const conv = rec().conversation("t-i2");
    const client = makeClient([
      {
        when: () => true,
        respond: () => ({
          content: null,
          tool_calls: [tc("s1", "shell", { command: "echo round1" })],
        }),
      },
      { when: () => true, respond: () => ({ content: "第一段总结" }) },
    ]);
    CFG.task_max_turns = 2;
    await solveTask(client, "t-i2", "任务卡", 600, conv, {});
    // 真失败重启：不传 messages → 新上下文（第二条 system 有意）
    const r2 = await solveTask(client, "t-i2", "任务卡", 600, conv, {});
    expect(r2.messages[0].role).toBe("system");

    const recs = convOf("t-i2");
    expect(count(recs, (r) => r.kind === "system")).toBe(2); // 有意：失败重启新上下文
    // rebuild 从最后一次 system 起 → 发送序列里 system 恰一条（无重复进入对话）
    const rebuilt = rec().loadMessages("t-i2");
    expect(rebuilt.filter((m) => m.role === "system").length).toBe(1);
  });
});

describe("fork 模式完整性", () => {
  it("fork/core_log/broadcast/flag 各恰一条；续段与 rebuild 无双份", async () => {
    CFG.fork_enabled = true;
    const conv = rec().conversation("t-i3");
    const client = makeClient([
      // trunk t1：fork
      {
        when: (_m, br, own) => br === "trunk" && own === 0,
        respond: () => ({
          content: null,
          tool_calls: [tc("f1", "fork", { direction: "redis 方向" })],
        }),
      },
      // b1 t1：core_log + shell(submit 响应)
      {
        when: (_m, br, own) => br === "b1" && own === 0,
        respond: () => ({
          content: null,
          tool_calls: [
            tc("c1", "core_log", { note: "redis 无认证" }),
            tc("s1", "shell", { command: `echo '${FLAG_JSON}'` }),
          ],
        }),
      },
      // trunk t2：broadcast 给 b1
      {
        when: (_m, br, own) => br === "trunk" && own === 1,
        respond: () => ({
          content: null,
          tool_calls: [tc("b1c", "broadcast", { note: "redis 拿到 idx1", to: [1] })],
        }),
      },
      // b1 t2 / trunk t3：park
      { when: () => true, respond: () => ({ content: "收束" }) },
    ]);
    const r1 = await solveTask(client, "t-i3", "卡", 600, conv, {
      taskClass: "short",
      flagTotal: 1,
    });
    expect(r1.status).toBe("done");

    let recs = convOf("t-i3");
    // 精确计数：每类恰一条
    expect(count(recs, (r) => r.kind_note === "fork_directive")).toBe(1);
    expect(count(recs, (r) => r.kind === "system")).toBe(1);
    expect(count(recs, (r) => r.kind === "tool" && r.tool === "core_log")).toBe(1);
    expect(count(recs, (r) => r.kind === "tool" && r.tool === "broadcast")).toBe(1);
    expect(count(recs, (r) => r.kind_note === "broadcast")).toBe(1); // 注入 b1 恰一条
    expect(
      count(
        readCorelog(path.join(recdir, "tasks", "t-i3", "corelog.jsonl")),
        (e) => e.kind === "flag",
      ),
    ).toBe(1);
    assertNoDuplicateContent(recs);

    // 内存 messages 的注入消息也恰一条（conv 与 messages 一致，不双份）
    const b1Msgs = r1.branches[0].messages;
    expect(b1Msgs.filter((m) => String(m.content ?? "").includes("[分支 0 广播]")).length).toBe(1);
    expect(b1Msgs.filter((m) => String(m.content ?? "").startsWith("【分支 1")).length).toBe(1);

    // 续段：messages+branches 传递再跑一轮 → conv 不新增 system/directive/广播
    CFG.task_max_turns = 3;
    const r2 = await solveTask(client, "t-i3", "卡", 600, conv, {
      messages: r1.messages,
      branches: r1.branches,
      startTurn: r1.turnsUsed,
      taskClass: "short",
      flagTotal: 1,
    });
    recs = convOf("t-i3");
    expect(count(recs, (r) => r.kind === "system")).toBe(1);
    expect(count(recs, (r) => r.kind_note === "fork_directive")).toBe(1);
    expect(count(recs, (r) => r.kind_note === "broadcast")).toBe(1);
    void r2;

    // rebuild（续跑场景）与内存 messages 等价：trunk 无分支记录、分支 directive 恰一条
    const trunk = rec().loadMessages("t-i3");
    expect(trunk.filter((m) => String(m.content ?? "").startsWith("【分支")).length).toBe(0);
    const brs = rec().loadBranches("t-i3");
    expect(brs.length).toBe(1);
    expect(
      brs[0].messages.filter((m) => String(m.content ?? "").startsWith("【分支 1")).length,
    ).toBe(1);
    expect(
      brs[0].messages.filter((m) => String(m.content ?? "").includes("[分支 0 广播]")).length,
    ).toBe(1);
    // 分支上下文里父分支快照的 system 仍恰一条
    expect(brs[0].messages.filter((m) => m.role === "system").length).toBe(1);
  });
});

describe("旁观者模式完整性", () => {
  it("插嘴 messages/conv 各恰一条；续段不重复；冷却内不二次注入", async () => {
    CFG.bystander_enabled = true;
    CFG.stall_short_sec = 0; // 停滞信号恒成立
    const conv = rec().conversation("t-i4");
    let n = 0;
    const client: LlmClient = {
      async create(opts) {
        const sys = String(opts.messages.find((m) => m.role === "system")?.content ?? "");
        if (sys === BYSTANDER_SYSTEM) {
          return {
            choices: [{ message: { role: "assistant", content: "换抽象层审计 JS。" } }],
          };
        }
        n++;
        // 重复命令维持 repeat 信号（第 3 次后凑齐二信号触发旁观者）
        return {
          choices: [
            {
              message: {
                role: "assistant",
                content: null,
                tool_calls: [tc(`s${n}`, "shell", { command: "echo looping" })],
              },
            },
          ],
        };
      },
    };
    // 段1：max_turns=4（前 3 轮重复命令 → 第 4 轮前触发注入 → 第 4 轮仍重复）
    CFG.task_max_turns = 4;
    const r1 = await solveTask(client, "t-i4", "卡", 600, conv, { taskClass: "short" });
    expect(
      r1.messages.filter((m) => String(m.content ?? "").startsWith("[旁观者提示]")).length,
    ).toBe(1);
    let recs = convOf("t-i4");
    expect(count(recs, (r) => r.kind_note === "bystander")).toBe(1);
    expect(
      count(
        readCorelog(path.join(recdir, "tasks", "t-i4", "corelog.jsonl")),
        (e) => e.kind === "bystander",
      ),
    ).toBe(1);
    assertNoDuplicateContent(recs);

    // 续段：messages 传递再跑 3 轮——冷却（400s）远超测试时长 → 不得二次注入
    const nBefore = n;
    CFG.task_max_turns = 3;
    const r2 = await solveTask(client, "t-i4", "卡", 600, conv, {
      messages: r1.messages,
      startTurn: r1.turnsUsed,
      taskClass: "short",
    });
    expect(n).toBe(nBefore + 3); // 恰 3 次主对话调用，无额外旁观者调用
    expect(
      r2.messages.filter((m) => String(m.content ?? "").startsWith("[旁观者提示]")).length,
    ).toBe(1);
    recs = convOf("t-i4");
    expect(count(recs, (r) => r.kind_note === "bystander")).toBe(1); // conv 不新增
    expect(count(recs, (r) => r.kind === "system")).toBe(1);
    assertNoDuplicateContent(recs);

    // rebuild 后插嘴也恰一条（续跑不双份）
    const rebuilt = rec().loadMessages("t-i4");
    expect(rebuilt.filter((m) => String(m.content ?? "").startsWith("[旁观者提示]")).length).toBe(
      1,
    );
  });
});
