/** solveTask 的 fork/旁观者集成自测（假 LLM client，全离线）。
 *
 * 覆盖：fork 生命周期（reserve→materialize→轮转→broadcast→park→收束）、
 * core_log/flag 账本落盘、旁观者触发注入、剪枝摘要注入、续段分支重建。
 * 平台层不需要（直接调 solveTask，不走 run()）。 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { solveTask } from "../src/agent.js";
import { BYSTANDER_SYSTEM } from "../src/bystander.js";
import { CFG } from "../src/config.js";
import { readCorelog } from "../src/corelog.js";
import { initRecords, type RunRecords, resetRecords, setRecordsDir } from "../src/records.js";
import { setTaskCwd } from "../src/shell.js";
import type { ChatMessage, LlmClient } from "../src/types.js";

// 配置快照/恢复（CFG 是模块级可变对象）
const SNAP = { ...CFG };

beforeEach(() => {
  Object.assign(CFG, SNAP);
  CFG.model = "deepseek-flash"; // 测试不走 resolveAsync，直接给模型名
});

afterEach(() => {
  Object.assign(CFG, SNAP);
  resetRecords();
  setTaskCwd("");
});

/** 脚本化假 client：按分支上下文（directive 含「分支 N」）与工具调用数派发响应。 */
function makeScriptedClient(
  script: Array<{
    when: (msgs: ChatMessage[], tools: unknown[] | undefined) => boolean;
    respond: (msgs: ChatMessage[]) => {
      content?: string | null;
      tool_calls?: Array<{
        id: string;
        type: "function";
        function: { name: string; arguments: string };
      }>;
    };
  }>,
) {
  const calls: Array<{ branchHint: string; hasBystander: boolean; msgs: ChatMessage[] }> = [];
  const client: LlmClient = {
    async create(opts) {
      const msgs = opts.messages;
      const sys = String(msgs.find((m) => m.role === "system")?.content ?? "");
      const isBystander = sys === BYSTANDER_SYSTEM;
      const directive = msgs.find((m) => String(m.content ?? "").startsWith("【分支"));
      const branchId = directive ? /【分支 (\d+)/.exec(String(directive.content))?.[1] : null;
      const branchHint = branchId ? `b${branchId}` : "trunk";
      calls.push({ branchHint, hasBystander: isBystander, msgs: [...msgs] });
      // 旁观者轮（useTools=false）
      if (isBystander || opts.tools === undefined) {
        return {
          choices: [
            {
              message: {
                role: "assistant",
                content: "放弃当前爆破，转而审计前端 JS 源码里的密钥与接口。",
              },
            },
          ],
        };
      }
      for (const s of script) {
        if (s.when(msgs, opts.tools)) {
          const r = s.respond(msgs);
          return { choices: [{ message: { role: "assistant", ...r } }] };
        }
      }
      throw new Error(`fake client: no script matched (call #${calls.length})`);
    },
  };
  return { client, calls };
}

const okResp = {
  choices: [{ message: { role: "assistant", content: "ok" } }],
};

function tc(id: string, name: string, args: Record<string, unknown>) {
  return {
    id,
    type: "function" as const,
    function: { name, arguments: JSON.stringify(args) },
  };
}

describe("solveTask fork 集成", () => {
  it("fork→core_log/flag→broadcast→双分支 park→done（含记录与重建断言）", async () => {
    CFG.fork_enabled = true;
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "fork_e2e_"));
    setRecordsDir(recdir);
    const R: RunRecords = initRecords("local", "deepseek-flash");
    const conv = R.conversation("t-f1");
    const workdir = fs.mkdtempSync(path.join(os.tmpdir(), "fork_work_"));
    setTaskCwd(workdir);

    const flagJson =
      '{"correct":true,"matched_flag_index":1,"correct_flag_count":1,"total_flag_count":2}';
    // 分支上下文里继承了父分支的工具结果 → 只数 directive 之后的（本分支自己的）工具轮
    const ownTools = (m: ChatMessage[]): number => {
      const idx = m.findIndex((x) => String(x.content ?? "").startsWith("【分支"));
      const from = idx >= 0 ? idx : 0;
      return m.slice(from).filter((x) => x.role === "tool").length;
    };
    // 脚本按「分支上下文 + 本分支工具结果数」派发
    const { client, calls } = makeScriptedClient([
      // trunk t1：fork
      {
        when: (m) =>
          !m.some((x) => String(x.content ?? "").startsWith("【分支")) && ownTools(m) === 0,
        respond: () => ({
          content: null,
          tool_calls: [tc("f1", "fork", { direction: "redis 6379 未授权排查" })],
        }),
      },
      // branch 1 t1：core_log + shell（submit 响应含 matched_flag_index）
      {
        when: (m) =>
          m.some((x) => String(x.content ?? "").startsWith("【分支 1")) && ownTools(m) === 0,
        respond: () => ({
          content: null,
          tool_calls: [
            tc("c1", "core_log", { note: "redis PONG 无认证" }),
            tc("s1", "shell", { command: `echo '${flagJson}'` }),
          ],
        }),
      },
      // trunk t2：broadcast 给分支 1
      {
        when: (m) =>
          !m.some((x) => String(x.content ?? "").startsWith("【分支")) && ownTools(m) === 1,
        respond: () => ({
          content: null,
          tool_calls: [tc("b1", "broadcast", { note: "redis 无认证，别重启服务", to: [1] })],
        }),
      },
      // branch 1 t2：确认收到广播后 park（无工具调用）
      {
        when: (m) => m.some((x) => String(x.content ?? "").startsWith("【分支 1")),
        respond: (m) => {
          expect(m.some((x) => String(x.content ?? "").includes("[分支 0 广播]"))).toBe(true);
          return { content: "收到，继续深挖 redis。" };
        },
      },
      // trunk t3：park
      {
        when: () => true,
        respond: () => ({ content: "主线收束，等分支汇报。" }),
      },
    ]);

    const r = await solveTask(client, "t-f1", "任务卡：拿 flag", 600, conv, {
      taskClass: "short",
      flagTotal: 2,
    });

    expect(r.status).toBe("done");
    // 调用顺序：trunk→b1→trunk→b1→trunk（轮转）
    expect(calls.map((c) => c.branchHint)).toEqual(["trunk", "b1", "trunk", "b1", "trunk"]);
    // 分支上下文协议完整（无孤儿 tool_call；多个结果跟同一 assistant 合法）
    for (const c of calls) {
      const pending = new Set<string>();
      for (const m of c.msgs) {
        if (m.role === "assistant" && m.tool_calls?.length) {
          for (const t of m.tool_calls) pending.add(t.id);
        } else if (m.role === "tool") {
          expect(pending.has(m.tool_call_id ?? ""), "orphan tool").toBe(true);
          pending.delete(m.tool_call_id ?? "");
        }
      }
    }
    // 分支上下文里含 fork 工具结果（materialize 在轮末，拷贝含完整配对）
    const b1First = calls[1];
    expect(JSON.stringify(b1First.msgs)).toContain("branch 1 reserved");

    // 记录：corelog 有 core/flag/broadcast/fork 四类
    const log = readCorelog(path.join(recdir, "tasks", "t-f1", "corelog.jsonl"));
    const kinds = log.map((e) => `${e.kind}:${e.branch}`);
    expect(kinds).toContain("core:1");
    expect(kinds).toContain("flag:1");
    expect(kinds).toContain("broadcast:0");
    expect(kinds).toContain("fork:0");
    const flagEntry = log.find((e) => e.kind === "flag")!;
    expect(flagEntry.flag_index).toBe(1);
    expect(flagEntry.total_flag_count).toBe(2);

    // 记录：conversation 分支字段齐全（fork_directive、broadcast 注入、flag 轮）
    const convLines = fs
      .readFileSync(path.join(recdir, "tasks", "t-f1", "conversation.jsonl"), "utf8")
      .split("\n")
      .filter(Boolean)
      .map((l) => JSON.parse(l) as Record<string, unknown>);
    expect(convLines.some((r) => r.kind_note === "fork_directive" && r.branch === 1)).toBe(true);
    expect(convLines.some((r) => r.kind_note === "broadcast" && r.branch === 1)).toBe(true);
    expect(convLines.some((r) => r.kind === "tool" && r.branch === 1 && r.tool === "shell")).toBe(
      true,
    );
    // trunk 工具结果的 branch=0
    expect(
      convLines.some((r) => r.kind === "tool" && r.branch === 0 && r.tool === "broadcast"),
    ).toBe(true);

    // 分支返回给 worker（跨段）：branch 1 存活且上下文完整
    expect(r.branches.length).toBe(1);
    expect(r.branches[0].id).toBe(1);
    expect(r.branches[0].label).toContain("redis");

    // 续段重建：trunk 排除分支记录；分支 = trunk 快照 + 自己的轮次
    const trunkRebuilt = R.loadMessages("t-f1");
    expect(trunkRebuilt.some((m) => String(m.content ?? "").includes("【分支 1"))).toBe(false);
    const brRebuilt = R.loadBranches("t-f1");
    expect(brRebuilt.length).toBe(1);
    const b1 = brRebuilt[0];
    expect(b1.messages[0].role).toBe("system");
    expect(b1.messages.some((m) => String(m.content ?? "").includes("redis PONG 无认证"))).toBe(
      true,
    );
    // fork 之后 trunk 的 park 轮（t3）不得进入分支 1
    expect(b1.messages.some((m) => String(m.content ?? "").includes("主线收束"))).toBe(false);
    // 协议完整（pending 集合口径）
    {
      const pending = new Set<string>();
      for (const m of b1.messages) {
        if (m.role === "assistant" && m.tool_calls?.length) {
          for (const t of m.tool_calls) pending.add(t.id);
        } else if (m.role === "tool") {
          expect(pending.has(m.tool_call_id ?? ""), "rebuild orphan tool").toBe(true);
          pending.delete(m.tool_call_id ?? "");
        }
      }
    }
  });

  it("fork 被护栏拒绝时返回原因，不建分支", async () => {
    CFG.fork_enabled = true;
    CFG.fork_max = 0; // 总数上限 0 → 立即拒绝
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "fork_deny_"));
    setRecordsDir(recdir);
    const R = initRecords("local", "deepseek-flash");
    const conv = R.conversation("t-f2");
    const { client, calls } = makeScriptedClient([
      {
        when: (m) => m.filter((x) => x.role === "tool").length === 0,
        respond: () => ({
          content: null,
          tool_calls: [tc("f1", "fork", { direction: "x" })],
        }),
      },
      {
        when: () => true,
        respond: () => ({ content: "收束。" }),
      },
    ]);
    const r = await solveTask(client, "t-f2", "卡", 600, conv, {});
    expect(r.status).toBe("done");
    expect(r.branches.length).toBe(0); // 没建分支
    // 拒绝结果出现在下一轮的上下文里（工具结果在响应后回填）
    const tail = JSON.stringify(calls[1]?.msgs ?? calls[0].msgs);
    expect(tail).toContain("fork refused");
  });

  it("旁观者：停滞+重复信号触发 → 注入 [旁观者提示] 且限频生效", async () => {
    CFG.fork_enabled = false;
    CFG.bystander_enabled = true;
    CFG.stall_short_sec = 0; // 停滞信号恒成立，靠重复信号凑齐任二
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "by_e2e_"));
    setRecordsDir(recdir);
    const R = initRecords("local", "deepseek-flash");
    const conv = R.conversation("t-by1");

    let n = 0;
    const client: LlmClient = {
      async create(opts) {
        const sys = String(opts.messages.find((m) => m.role === "system")?.content ?? "");
        if (sys === BYSTANDER_SYSTEM) {
          return okResp; // 旁观者回复（注入体在下方断言里核对）
        }
        n++;
        if (n <= 3) {
          // 三次相同命令 → repeat 信号（echo 离线安全）
          return {
            choices: [
              {
                message: {
                  role: "assistant",
                  content: null,
                  tool_calls: [tc(`s${n}`, "shell", { command: "echo repeating-scan" })],
                },
              },
            ],
          };
        }
        // 第 4 轮：模型看到旁观者插嘴
        const sawBystander = opts.messages.some((m) =>
          String(m.content ?? "").startsWith("[旁观者提示]"),
        );
        expect(sawBystander, "bystander message injected").toBe(true);
        return { choices: [{ message: { role: "assistant", content: "换方向审计 JS。" } }] };
      },
    };

    const r = await solveTask(client, "t-by1", "卡", 600, conv, { taskClass: "short" });
    expect(r.status).toBe("done");

    // 记录：bystander user 条目（kind_note）+ corelog bystander 事件
    const convLines = fs
      .readFileSync(path.join(recdir, "tasks", "t-by1", "conversation.jsonl"), "utf8")
      .split("\n")
      .filter(Boolean)
      .map((l) => JSON.parse(l) as Record<string, unknown>);
    const byRec = convLines.find((x) => x.kind_note === "bystander");
    expect(byRec).toBeTruthy();
    expect(String(byRec!.content)).toContain("[旁观者提示]");
    const log = readCorelog(path.join(recdir, "tasks", "t-by1", "corelog.jsonl"));
    expect(log.some((e) => e.kind === "bystander")).toBe(true);
  });

  it("旁观者：冷却期内不重复注入", async () => {
    CFG.fork_enabled = false;
    CFG.bystander_enabled = true;
    CFG.stall_short_sec = 0;
    CFG.bystander_cooldown_sec = 3600; // 冷却远超测试时长
    CFG.task_max_turns = 8; // 轮数安全网快速收段
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "by_cd_"));
    setRecordsDir(recdir);
    const R = initRecords("local", "deepseek-flash");
    const conv = R.conversation("t-by2");
    let bystanderCalls = 0;
    let n = 0;
    const client: LlmClient = {
      async create(opts) {
        const sys = String(opts.messages.find((m) => m.role === "system")?.content ?? "");
        if (sys === BYSTANDER_SYSTEM) {
          bystanderCalls++;
          return okResp;
        }
        n++;
        // 持续重复命令（repeat 信号保持成立）
        return {
          choices: [
            {
              message: {
                role: "assistant",
                content: null,
                tool_calls: [tc(`s${n}`, "shell", { command: "echo repeating-scan" })],
              },
            },
          ],
        };
      },
    };
    const r = await solveTask(client, "t-by2", "卡", 600, conv, { taskClass: "short" });
    expect(r.status).toBe("paused"); // 轮数安全网收段
    expect(bystanderCalls).toBe(1); // 第一次之后冷却期内不再注入
  });

  it("剪枝：分支零核心事件达阈值 → 回收 + 摘要注入 trunk", async () => {
    CFG.fork_enabled = true;
    CFG.branch_prune_sec = 0; // 立即满足剪枝条件
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "prune_"));
    setRecordsDir(recdir);
    const R = initRecords("local", "deepseek-flash");
    const conv = R.conversation("t-pr1");
    const workdir = fs.mkdtempSync(path.join(os.tmpdir(), "prune_work_"));
    setTaskCwd(workdir);
    const { client } = makeScriptedClient([
      {
        when: (m) =>
          !m.some((x) => String(x.content ?? "").startsWith("【分支")) &&
          m.filter((x) => x.role === "tool").length === 0,
        respond: () => ({
          content: null,
          tool_calls: [tc("f1", "fork", { direction: "注定被剪枝的方向" })],
        }),
      },
      {
        when: () => true,
        respond: () => ({ content: "trunk 继续。" }),
      },
    ]);
    const r = await solveTask(client, "t-pr1", "卡", 600, conv, {});
    expect(r.status).toBe("done");
    expect(r.branches.length).toBe(0); // 分支被剪掉
    // trunk 收到回收摘要
    expect(r.messages.some((m) => String(m.content ?? "").includes("[分支 1 已回收]"))).toBe(true);
    expect(r.messages.some((m) => String(m.content ?? "").includes("注定被剪枝的方向"))).toBe(true);
    // 记录：prune 事件
    const log = readCorelog(path.join(recdir, "tasks", "t-pr1", "corelog.jsonl"));
    expect(log.some((e) => e.kind === "prune" && e.branch === 1)).toBe(true);
    const convLines = fs
      .readFileSync(path.join(recdir, "tasks", "t-pr1", "conversation.jsonl"), "utf8")
      .split("\n")
      .filter(Boolean)
      .map((l) => JSON.parse(l) as Record<string, unknown>);
    expect(convLines.some((x) => x.kind_note === "branch_pruned" && x.branch === 0)).toBe(true);
  });

  it("开关全关时与旧单分支行为一致（无 branch 工具、无 corelog）", async () => {
    CFG.fork_enabled = false;
    CFG.bystander_enabled = false;
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "off_e2e_"));
    setRecordsDir(recdir);
    const R = initRecords("local", "deepseek-flash");
    const conv = R.conversation("t-off");
    const client: LlmClient = {
      async create(opts) {
        // 基础工具集 = shell + submit_flag（submit 常驻唯一提交通道）；
        // branch 工具（fork/core_log/broadcast）与 corelog 在开关全关时不得出现
        expect(opts.tools?.length).toBe(2);
        expect(JSON.stringify(opts.tools)).toContain("shell");
        expect(JSON.stringify(opts.tools)).toContain("submit_flag");
        expect(JSON.stringify(opts.tools)).not.toContain("fork");
        expect(JSON.stringify(opts.tools)).not.toContain("core_log");
        expect(JSON.stringify(opts.tools)).not.toContain("broadcast");
        const toolMsgs = opts.messages.filter((m) => m.role === "tool").length;
        if (toolMsgs === 0) {
          return {
            choices: [
              {
                message: {
                  role: "assistant",
                  content: null,
                  tool_calls: [tc("s1", "shell", { command: "echo hi" })],
                },
              },
            ],
          };
        }
        return { choices: [{ message: { role: "assistant", content: "完成。" } }] };
      },
    };
    const r = await solveTask(client, "t-off", "卡", 600, conv, {});
    expect(r.status).toBe("done");
    expect(r.branches.length).toBe(0);
    // 无 corelog 文件（多分支基础设施未启用）
    expect(fs.existsSync(path.join(recdir, "tasks", "t-off", "corelog.jsonl"))).toBe(false);
    // system prompt 与旧版逐字节一致
    const sysRec = fs
      .readFileSync(path.join(recdir, "tasks", "t-off", "conversation.jsonl"), "utf8")
      .split("\n")
      .filter(Boolean)
      .map((l) => JSON.parse(l) as Record<string, unknown>)
      .find((x) => x.kind === "system")!;
    const { SYSTEM_PROMPT } = await import("../src/prompt.js");
    expect(sysRec.content).toBe(SYSTEM_PROMPT);
  });
});
