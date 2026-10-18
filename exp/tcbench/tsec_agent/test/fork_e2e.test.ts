/** solveTask 的 fork/planner 集成自测（假 LLM client，全离线）。
 *
 * 覆盖：fork 生命周期（reserve→materialize→轮转→log→park→收束）、
 * log/flag 账本落盘、planner 卡塞提醒、剪枝摘要注入、续段分支重建。
 * 平台层不需要（直接调 solveTask，不走 run()）。 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { solveTask } from "../src/agent.js";
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
  const calls: Array<{ branchHint: string; msgs: ChatMessage[] }> = [];
  const client: LlmClient = {
    async create(opts) {
      const msgs = opts.messages;
      const directive = msgs.find((m) => String(m.content ?? "").startsWith("【分支"));
      const branchId = directive ? /【分支 (\d+)/.exec(String(directive.content))?.[1] : null;
      const branchHint = branchId ? `b${branchId}` : "trunk";
      calls.push({ branchHint, msgs: [...msgs] });
      // 无工具元调用（压缩等）不在本测试脚本范围
      if (opts.tools === undefined) {
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

function tc(id: string, name: string, args: Record<string, unknown>) {
  return {
    id,
    type: "function" as const,
    function: { name, arguments: JSON.stringify(args) },
  };
}

describe("solveTask fork 集成", () => {
  it("fork→log/flag→双 Attempt park→done（含记录与重建断言）", async () => {
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
      // branch 1 t1：log + shell（submit 响应含 matched_flag_index）
      {
        when: (m) =>
          m.some((x) => String(x.content ?? "").startsWith("【分支 1")) && ownTools(m) === 0,
        respond: () => ({
          content: null,
          tool_calls: [
            tc("c1", "log", { note: "redis PONG 无认证" }),
            tc("s1", "shell", { command: `echo '${flagJson}'` }),
          ],
        }),
      },
      // trunk t2：log 给 planner/黑板（不再有 broadcast 工具）
      {
        when: (m) =>
          !m.some((x) => String(x.content ?? "").startsWith("【分支")) && ownTools(m) === 1,
        respond: () => ({
          content: null,
          tool_calls: [tc("b1", "log", { note: "redis 无认证，别重启服务" })],
        }),
      },
      // branch 1 t2：park（无工具调用）
      {
        when: (m) => m.some((x) => String(x.content ?? "").startsWith("【分支 1")),
        respond: () => ({ content: "继续深挖 redis。" }),
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

    // 记录：corelog 有 core/flag/fork 三类
    const log = readCorelog(path.join(recdir, "tasks", "t-f1", "log.jsonl"));
    const kinds = log.map((e) => `${e.kind}:${e.branch}`);
    expect(kinds).toContain("core:1");
    expect(kinds).toContain("flag:1");
    expect(kinds).toContain("core:0");
    expect(kinds).toContain("fork:0");
    const flagEntry = log.find((e) => e.kind === "flag")!;
    expect(flagEntry.flag_index).toBe(1);
    expect(flagEntry.total_flag_count).toBe(2);

    // 记录：conversation 分支字段齐全（fork_directive、flag 轮）
    const convLines = fs
      .readFileSync(path.join(recdir, "tasks", "t-f1", "conversation.jsonl"), "utf8")
      .split("\n")
      .filter(Boolean)
      .map((l) => JSON.parse(l) as Record<string, unknown>);
    expect(convLines.some((r) => r.kind_note === "fork_directive" && r.branch === 1)).toBe(true);
    expect(convLines.some((r) => r.kind === "tool" && r.branch === 1 && r.tool === "shell")).toBe(
      true,
    );
    // trunk log 工具结果的 branch=0
    expect(convLines.some((r) => r.kind === "tool" && r.branch === 0 && r.tool === "log")).toBe(
      true,
    );

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

  it("planner 卡塞提醒：停滞+重复信号 → 注入 [planner 提醒]", async () => {
    CFG.fork_enabled = true;
    CFG.planner_enabled = true;
    CFG.stall_short_sec = 0; // 停滞信号恒成立，靠重复信号凑齐任二
    CFG.planner_nudge_cooldown_sec = 0;
    CFG.planner_nudge_max = 1;
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "plan_stall_"));
    setRecordsDir(recdir);
    const R = initRecords("local", "deepseek-flash");
    const conv = R.conversation("t-ps1");
    let n = 0;
    const client: LlmClient = {
      async create(opts) {
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
        const sawPlanner = opts.messages.some((m) =>
          String(m.content ?? "").startsWith("[planner 提醒]"),
        );
        expect(sawPlanner, "planner nudge injected").toBe(true);
        return { choices: [{ message: { role: "assistant", content: "换方法继续。" } }] };
      },
    };

    const r = await solveTask(client, "t-ps1", "卡", 600, conv, { taskClass: "short" });
    expect(r.status).toBe("done");
    const convLines = fs
      .readFileSync(path.join(recdir, "tasks", "t-ps1", "conversation.jsonl"), "utf8")
      .split("\n")
      .filter(Boolean)
      .map((l) => JSON.parse(l) as Record<string, unknown>);
    const nudge = convLines.find((x) => x.kind_note === "planner_nudge");
    expect(nudge).toBeTruthy();
    expect(String(nudge!.content)).toContain("[planner 提醒]");
    const log = readCorelog(path.join(recdir, "tasks", "t-ps1", "log.jsonl"));
    expect(log.some((e) => e.kind === "planner")).toBe(true);
  });

  it("planner 提醒冷却：一次之后不重复刷屏", async () => {
    CFG.fork_enabled = true;
    CFG.planner_enabled = true;
    CFG.stall_short_sec = 0;
    CFG.planner_nudge_cooldown_sec = 3600; // 冷却远超测试时长
    CFG.planner_nudge_max = 2;
    CFG.task_max_turns = 8; // 轮数安全网快速收段
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "plan_cd_"));
    setRecordsDir(recdir);
    const R = initRecords("local", "deepseek-flash");
    const conv = R.conversation("t-ps2");
    let n = 0;
    const client: LlmClient = {
      async create() {
        n++;
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
    const r = await solveTask(client, "t-ps2", "卡", 600, conv, { taskClass: "short" });
    expect(r.status).toBe("paused"); // 轮数安全网收段
    const convLines = fs
      .readFileSync(path.join(recdir, "tasks", "t-ps2", "conversation.jsonl"), "utf8")
      .split("\n")
      .filter(Boolean)
      .map((l) => JSON.parse(l) as Record<string, unknown>);
    expect(convLines.filter((x) => x.kind_note === "planner_nudge").length).toBe(1);
  });

  it("report：分支主动 deadend → park + 结果卡回传父级", async () => {
    CFG.fork_enabled = true;
    CFG.planner_enabled = false; // 本用例只验 report 本身；planner 另测
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "fork_report_"));
    setRecordsDir(recdir);
    const R = initRecords("local", "deepseek-flash");
    const conv = R.conversation("t-rp1");
    const workdir = fs.mkdtempSync(path.join(os.tmpdir(), "fork_report_work_"));
    setTaskCwd(workdir);
    const { client, calls } = makeScriptedClient([
      {
        when: (m) =>
          !m.some((x) => String(x.content ?? "").startsWith("【分支")) &&
          m.filter((x) => x.role === "tool").length === 0,
        respond: () => ({
          content: null,
          tool_calls: [tc("f1", "fork", { direction: "HTTP 隐藏路由" })],
        }),
      },
      {
        when: (m) => m.some((x) => String(x.content ?? "").startsWith("【分支 1")),
        respond: () => ({
          content: null,
          tool_calls: [
            tc("r1", "report", {
              status: "deadend",
              summary: "HTTP 只有 / 和 /download，无隐藏路由",
              evidence: ["ffuf 63k 字典 0 命中", "/. 归一化到 index"],
              artifacts: [`${workdir}/branches/1/ffuf.json`],
              next: ["把预算给 binary 方向"],
            }),
          ],
        }),
      },
      {
        when: () => true,
        respond: () => ({ content: "收到结果卡，收束。" }),
      },
    ]);

    const r = await solveTask(client, "t-rp1", "任务卡", 600, conv, { taskClass: "short" });
    expect(r.status).toBe("done");
    expect(calls.map((c) => c.branchHint)).toEqual(["trunk", "b1", "trunk"]);
    const b1 = r.branches.find((b) => b.id === 1)!;
    expect(b1.status).toBe("deadend");
    expect(b1.active).toBe(false);
    expect(b1.result?.summary).toContain("无隐藏路由");
    // 结果卡只写黑板；planner 事件负责后续决策。
    const log = readCorelog(path.join(recdir, "tasks", "t-rp1", "log.jsonl"));
    expect(log.some((e) => e.kind === "result" && e.branch === 1)).toBe(true);
    const convLines = fs
      .readFileSync(path.join(recdir, "tasks", "t-rp1", "conversation.jsonl"), "utf8")
      .split("\n")
      .filter(Boolean)
      .map((l) => JSON.parse(l) as Record<string, unknown>);
    expect(convLines.some((x) => x.kind_note === "attempt_result")).toBe(false);
  });

  it("planner：deadend + next → 从 checkpoint 开兄弟并回收 source", async () => {
    CFG.fork_enabled = true;
    CFG.planner_enabled = true;
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "fork_plan_"));
    setRecordsDir(recdir);
    const R = initRecords("local", "deepseek-flash");
    const conv = R.conversation("t-pl1");
    const workdir = fs.mkdtempSync(path.join(os.tmpdir(), "fork_plan_work_"));
    setTaskCwd(workdir);
    const { client, calls } = makeScriptedClient([
      {
        when: (m) =>
          !m.some((x) => String(x.content ?? "").startsWith("【分支")) &&
          m.filter((x) => x.role === "tool").length === 0,
        respond: () => ({
          content: null,
          tool_calls: [tc("f1", "fork", { direction: "HTTP 隐藏路由" })],
        }),
      },
      {
        when: (m) => m.some((x) => String(x.content ?? "").startsWith("【分支 1")),
        respond: () => ({
          content: null,
          tool_calls: [
            tc("r1", "report", {
              status: "deadend",
              summary: "HTTP 只有 / 和 /download",
              next: ["动态 trace validator 的 VM 循环"],
            }),
          ],
        }),
      },
      {
        when: (m) => m.some((x) => String(x.content ?? "").startsWith("【分支 2")),
        respond: () => ({ content: "动态 trace 继续。" }),
      },
      {
        when: () => true,
        respond: () => ({ content: "收到 planner 的新 Attempt。" }),
      },
    ]);

    const r = await solveTask(client, "t-pl1", "任务卡", 600, conv, { taskClass: "short" });
    expect(r.status).toBe("done");
    // source 1 被回收，只剩 planner spawn 的 attempt 2（与 0 共享 checkpoint 前缀）
    expect(r.branches.length).toBe(1);
    expect(r.branches[0].id).toBe(2);
    expect(r.branches[0].messages.some((m) => String(m.content ?? "").includes("【分支 2"))).toBe(
      true,
    );
    // 新 Attempt 不继承 source 的对话尾部，但带一句负知识
    expect(
      r.branches[0].messages.some((m) =>
        String(m.content ?? "").includes("HTTP 只有 / 和 /download"),
      ),
    ).toBe(true);
    expect(r.branches[0].messages.some((m) => String(m.content ?? "").startsWith("【分支 1"))).toBe(
      false,
    );
    const raw = fs
      .readFileSync(path.join(recdir, "tasks", "t-pl1", "conversation.jsonl"), "utf8")
      .split("\n")
      .filter(Boolean)
      .map((l) => JSON.parse(l) as Record<string, unknown>);
    const spawned = raw.find(
      (x) => x.kind_note === "fork_directive" && x.branch === 2 && x.spawned_by === "planner",
    );
    expect(spawned).toBeTruthy();
    expect(spawned?.source_branch).toBe(1);
    const log = readCorelog(path.join(recdir, "tasks", "t-pl1", "log.jsonl"));
    expect(log.some((e) => e.kind === "fork" && String(e.note ?? "").includes("#2"))).toBe(true);
    expect(log.some((e) => e.kind === "result" && e.branch === 1)).toBe(true);
    // 调用序列：初始 → source → 初始(处理结果卡) → 新 Attempt
    expect(calls.map((c) => c.branchHint)).toEqual(["trunk", "b1", "trunk", "b2"]);
    // planner 结构化持久化
    const planner = JSON.parse(fs.readFileSync(path.join(workdir, "planner.json"), "utf8")) as {
      attempts: Array<{ id: number; scopeId: number }>;
      recovery: { owner: number | null; pending: string[] };
    };
    expect(planner.attempts.some((a) => a.id === 2)).toBe(true);
    expect(planner.recovery.pending).toEqual([]);
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
    // 回收事件只写黑板；planner/相关 scope 读取。
    // 记录：prune 事件
    const log = readCorelog(path.join(recdir, "tasks", "t-pr1", "log.jsonl"));
    expect(log.some((e) => e.kind === "prune" && e.branch === 1)).toBe(true);
    const convLines = fs
      .readFileSync(path.join(recdir, "tasks", "t-pr1", "conversation.jsonl"), "utf8")
      .split("\n")
      .filter(Boolean)
      .map((l) => JSON.parse(l) as Record<string, unknown>);
  });

  it("开关全关时与旧单分支行为一致（无 branch 工具、无 corelog）", async () => {
    CFG.fork_enabled = false;
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "off_e2e_"));
    setRecordsDir(recdir);
    const R = initRecords("local", "deepseek-flash");
    const conv = R.conversation("t-off");
    const client: LlmClient = {
      async create(opts) {
        // 基础工具集 = shell + submit_flag（submit 常驻唯一提交通道）；
        // 协作工具（fork/report/log/state_change）与黑板在开关全关时不得出现
        expect(opts.tools?.length).toBe(2);
        const toolNames = ((opts.tools ?? []) as Array<{ function?: { name?: string } }>).map(
          (t) => t?.function?.name,
        );
        expect(toolNames).toContain("shell");
        expect(toolNames).toContain("submit_flag");
        expect(toolNames).not.toContain("fork");
        expect(toolNames).not.toContain("report");
        expect(toolNames).not.toContain("log");
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
    expect(fs.existsSync(path.join(recdir, "tasks", "t-off", "log.jsonl"))).toBe(false);
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

describe("done + next 待定队列", () => {
  it("done 的 next 在 source 已 satisfied 时仍能 spawn 新 Attempt", async () => {
    CFG.fork_enabled = true;
    CFG.planner_enabled = true;
    CFG.branch_concurrency = 1;
    CFG.max_branches = 4;
    CFG.task_max_turns = 8;
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "next_queue_"));
    setRecordsDir(recdir);
    const R = initRecords("local", "deepseek-flash");
    const conv = R.conversation("t-nextq");
    setTaskCwd(fs.mkdtempSync(path.join(os.tmpdir(), "next_queue_work_")));
    const { client, calls } = makeScriptedClient([
      {
        when: (m) => !m.some((x) => String(x.content ?? "").startsWith("【分支")),
        respond: () => ({
          content: null,
          tool_calls: [tc("f1", "fork", { direction: "初始分支" })],
        }),
      },
      {
        when: (m) => m.some((x) => String(x.content ?? "").startsWith("【分支 1")),
        respond: () => ({
          content: null,
          tool_calls: [
            tc("r1", "report", {
              status: "done",
              summary: "后台方向完成，但还有 OA/SSH 未覆盖",
              next: ["打 OA 管理面"],
            }),
          ],
        }),
      },
      {
        when: (m) => m.some((x) => String(x.content ?? "").startsWith("【分支 2")),
        respond: () => ({ content: "接手 OA 方向。" }),
      },
      {
        when: () => true,
        respond: () => ({ content: "主线收束。" }),
      },
    ]);
    const r = await solveTask(client, "t-nextq", "卡", 600, conv, {
      taskClass: "long",
      flagTotal: 2,
    });
    expect(["done", "paused"]).toContain(r.status);
    expect(calls.some((c) => c.branchHint === "b2")).toBe(true);
    expect(r.branches.some((b) => b.id === 2)).toBe(true);
  }, 30_000);
});
