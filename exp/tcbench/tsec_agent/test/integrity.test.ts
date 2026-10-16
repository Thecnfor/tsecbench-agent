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

describe("core_log 轮级轻推（执行层兜底）", () => {
  /** 每轮 shell、永不 core_log 的脚本。 */
  function shellOnlyClient() {
    let n = 0;
    const client: LlmClient = {
      async create(opts) {
        const sys = String(opts.messages.find((m) => m.role === "system")?.content ?? "");
        if (sys === BYSTANDER_SYSTEM) throw new Error("旁观者不该被触发（stall 阈值已放大）");
        n++;
        return {
          choices: [
            {
              message: {
                role: "assistant",
                content: null,
                tool_calls: [tc(`s${n}`, "shell", { command: `echo step${n}` })],
              },
            },
          ],
        };
      },
    };
    return { client };
  }

  it("连续 3 轮无 core_log → 第 4/8 轮注入提醒，每段恰 2 次", async () => {
    CFG.bystander_enabled = true; // multiOn（core_log 工具在场）但旁观者不触发
    CFG.stall_short_sec = 99999; // 放大全部时间信号，专注测轮级轻推
    const conv = rec().conversation("t-n1");
    const { client } = shellOnlyClient();
    CFG.task_max_turns = 8;
    await solveTask(client, "t-n1", "卡", 600, conv, { taskClass: "short" });

    const recs = convOf("t-n1");
    const nudged = recs.filter(
      (r) => r.kind === "tool" && String(r.result ?? "").includes("[系统提醒·核心日志]"),
    );
    expect(nudged.length).toBe(2); // 恰 2 次（上限）
    // 注入落在第 4、8 轮的工具结果上（turn 后缀）
    expect(String(nudged[0].turn)).toMatch(/:4$/);
    expect(String(nudged[1].turn)).toMatch(/:8$/);
    // messages 与 conv 一致（不双份不遗漏）
    // （conv 计数即忠实落盘；messages 内容由同一 result 变量写入）
  });

  it("该轮 core_log 清零 → 重新累计；多模态外常规模式绝不注入", async () => {
    CFG.bystander_enabled = true;
    CFG.stall_short_sec = 99999;
    const conv = rec().conversation("t-n2");
    let n = 0;
    const client: LlmClient = {
      async create(opts) {
        if (
          String(opts.messages.find((m) => m.role === "system")?.content ?? "") === BYSTANDER_SYSTEM
        ) {
          throw new Error("旁观者不该触发");
        }
        n++;
        // 轮 2 调 core_log（应把轮计数清零 → 提醒从轮 2 后重新数 3 轮 → 第 5 轮注入）
        const tcs =
          n === 2
            ? [tc("c1", "core_log", { note: "证据: 端口 8080 开放" })]
            : [tc(`s${n}`, "shell", { command: `echo r${n}` })];
        return {
          choices: [{ message: { role: "assistant", content: null, tool_calls: tcs } }],
        };
      },
    };
    CFG.task_max_turns = 6;
    await solveTask(client, "t-n2", "卡", 600, conv, { taskClass: "short" });
    const recs = convOf("t-n2");
    const nudged = recs.filter(
      (r) => r.kind === "tool" && String(r.result ?? "").includes("[系统提醒·核心日志]"),
    );
    expect(nudged.length).toBe(1); // 轮2 清零 → 重新累计3轮 → 轮6 注入（6 轮内恰一次）
    expect(String(nudged[0].turn)).toMatch(/:6$/);

    // 常规模式（multiOn 关）：跑 8 轮绝不注入（无 core_log 工具，注入会诱导 unknown tool）
    Object.assign(CFG, SNAP);
    CFG.model = "deepseek-flash";
    CFG.bystander_enabled = false;
    CFG.fork_enabled = false;
    CFG.task_max_turns = 8;
    const conv2 = rec().conversation("t-n3");
    const { client: plain } = shellOnlyClient();
    await solveTask(plain, "t-n3", "卡", 600, conv2, { taskClass: "short" });
    const recs2 = convOf("t-n3");
    expect(
      recs2.filter((r) => r.kind === "tool" && String(r.result ?? "").includes("[系统提醒")).length,
    ).toBe(0);
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
          // 轻调用关思考（省 completion：thinking 实测占成本大头）
          expect((opts.thinking as { type: string } | undefined)?.type).toBe("disabled");
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
