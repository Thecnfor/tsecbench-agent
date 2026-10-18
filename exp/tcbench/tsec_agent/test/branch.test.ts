/** branch.ts / corelog.ts / stall.ts / records 分支重建 的离线单测。
 * 全部纯逻辑，无网络、无 LLM。 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { type BranchCfg, BranchManager } from "../src/branch.js";
import { dispatchBranchTool } from "../src/branch_tools.js";
import {
  buildAttemptLogView,
  CorelogWriter,
  extractFlagIndex,
  FlagLedger,
  pendingMutations,
  readCorelog,
} from "../src/corelog.js";
import { FORK_PROMPT_EXTRA, FORK_SYSTEM_HINT, systemPromptWithExtras } from "../src/prompt.js";
import { rebuildBranches, rebuildMessages } from "../src/records.js";
import { evaluateStall, recentDigest, shouldIntervene } from "../src/stall.js";
import type { ChatMessage } from "../src/types.js";

const CFG: BranchCfg = {
  enabled: true,
  maxBranches: 3,
  maxForks: 5,
  forkCooldownSec: 180,
  pruneSec: 600,
  plannerNudgeMax: 2,
  plannerNudgeCooldownSec: 400,
};

function trunkMsgs(): ChatMessage[] {
  return [
    { role: "system", content: "sys" },
    { role: "user", content: "task card" },
    { role: "assistant", content: "ok" },
  ];
}
function mkBm(now = 1000): BranchManager {
  return new BranchManager(CFG, trunkMsgs(), now, "/work/tasks/x");
}

describe("BranchManager 护栏", () => {
  it("fork 未启用时拒绝", () => {
    const bm = new BranchManager({ ...CFG, enabled: false }, trunkMsgs(), 1000);
    expect(bm.reserveFork(0, 1000).ok).toBe(false);
  });

  it("存活分支上限（含 trunk）", () => {
    const bm = mkBm();
    expect(bm.reserveFork(0, 1001).ok).toBe(true);
    bm.materializeFork(1, 0, "方向A", 3, 1001, (id) => `/w/br/${id}`);
    expect(bm.reserveFork(0, 1200).ok).toBe(true); // 跳过冷却
    bm.materializeFork(2, 0, "方向B", 3, 1200, (id) => `/w/br/${id}`);
    const denied = bm.reserveFork(0, 1400);
    expect(denied.ok).toBe(false);
    if (!denied.ok) expect(denied.reason).toContain("上限 3");
  });

  it("fork 总数硬上限（剪枝后也不恢复）", () => {
    const bm = new BranchManager({ ...CFG, maxForks: 1 }, trunkMsgs(), 1000);
    expect(bm.reserveFork(0, 1001).ok).toBe(true);
    bm.materializeFork(1, 0, "A", 3, 1001, (id) => `/w/br/${id}`);
    bm.prune(1); // 回收后总数护栏仍生效
    expect(bm.reserveFork(0, 1002).ok).toBe(false);
  });

  it("冷却期内拒绝并告知剩余时间", () => {
    const bm = mkBm();
    expect(bm.reserveFork(0, 1100).ok).toBe(true);
    const denied = bm.reserveFork(0, 1150); // 冷却 180s，只过了 50s
    expect(denied.ok).toBe(false);
    if (!denied.ok) expect(denied.reason).toContain("130s");
    expect(bm.reserveFork(0, 1100 + 181).ok).toBe(true);
  });

  it("轮转调度：park/unpark 与全 parked 收束", () => {
    const bm = mkBm();
    bm.materializeFork(1, 0, "A", 3, 1001, (id) => `/w/br/${id}`);
    // 本用例只测 RR；清掉 fork 的一次性 boost
    bm.get(0)!.priority = 0;
    bm.get(1)!.priority = 0;
    const seen: number[] = [];
    for (let i = 0; i < 4; i++) {
      const b = bm.pick();
      if (b) seen.push(b.id);
    }
    // trunk 与 branch 1 交替（a1 b1 a2 b2 …）
    expect(seen).toEqual([0, 1, 0, 1]);
    bm.park(0);
    bm.park(1);
    expect(bm.pick()).toBeNull(); // 全 parked → 段收束
  });

  it("trunk 永不剪枝", () => {
    const bm = mkBm();
    expect(bm.prune(0)).toBeNull();
  });

  it("分支可再分叉（深度不设限，护栏兜底）", () => {
    const bm = mkBm();
    bm.materializeFork(1, 0, "A", 3, 1001, (id) => `/w/br/${id}`);
    const r = bm.reserveFork(1, 1200); // 分支 1 再分叉
    expect(r.ok).toBe(true);
    bm.materializeFork(2, 1, "A2", 5, 1200, (id) => `/w/br/${id}`);
    const b2 = bm.get(2);
    expect(b2?.parent).toBe(1);
    // 子分支继承父分支上下文（含父的 directive）
    expect(b2?.messages.some((m) => String(m.content).includes("分支 1 · 方向"))).toBe(true);
  });
});

describe("report / scope", () => {
  it("report deadend：park attempt、结果卡投递父级、scope refuted", () => {
    const bm = mkBm();
    bm.materializeFork(1, 0, "redis 未授权", 3, 1001, (id) => `/w/br/${id}`);
    const scopeId = bm.get(1)!.scopeId;
    const res = bm.report(
      1,
      "deadend",
      "redis 无认证但不可写，拿不到 flag",
      ["nmap 6379 open", "SET 返回 READONLY"],
      ["/w/br/1/scan.log"],
      ["试 6380"],
      1002,
    );
    expect(res.ok).toBe(true);
    expect(res.delivered).toEqual([]); // 不再自动回“父 Attempt”
    expect(res.card).toContain("[Attempt 1 已结束 · deadend]");
    const b1 = bm.get(1)!;
    expect(b1.status).toBe("deadend");
    expect(b1.active).toBe(false);
    expect(b1.result?.summary).toContain("redis");
    expect(bm.scopeStatus(scopeId)).toBe("refuted");
    expect(
      bm.get(0)!.messages.some((m) => String(m.content ?? "").includes("[Attempt 1 已结束")),
    ).toBe(false);
  });

  it("同 scope 多 attempt：任一 done=satisfied；全 deadend=refuted；blocked=unresolved", () => {
    const bm = mkBm();
    const sid = bm.newScope(0, "HTTP 入口组");
    bm.materializeFork(1, 0, "目录爆破", 3, 1001, (id) => `/w/br/${id}`, sid);
    bm.materializeFork(2, 0, "认证绕过", 3, 1002, (id) => `/w/br/${id}`, sid);
    expect(bm.scopeStatus(sid)).toBe("open");
    bm.report(1, "deadend", "目录无隐藏路由", [], [], [], 1003);
    expect(bm.scopeStatus(sid)).toBe("open"); // 2 还在跑
    bm.report(2, "done", "认证绕过拿到后台", [], [], [], 1004);
    expect(bm.scopeStatus(sid)).toBe("satisfied");

    const bm2 = mkBm();
    const sid2 = bm2.newScope(0, "端口组");
    bm2.materializeFork(1, 0, "A", 3, 1001, (id) => `/w/br/${id}`, sid2);
    bm2.materializeFork(2, 0, "B", 3, 1002, (id) => `/w/br/${id}`, sid2);
    bm2.report(1, "deadend", "A 死", [], [], [], 1003);
    bm2.report(2, "deadend", "B 死", [], [], [], 1004);
    expect(bm2.scopeStatus(sid2)).toBe("refuted");

    const bm3 = mkBm();
    const sid3 = bm3.newScope(0, "依赖组");
    bm3.materializeFork(1, 0, "C", 3, 1001, (id) => `/w/br/${id}`, sid3);
    bm3.report(1, "blocked", "靶机不可达", [], [], [], 1002);
    expect(bm3.scopeStatus(sid3)).toBe("unresolved");
  });

  it("report 幂等拒绝；restore 保留 status/result/active", () => {
    const bm = mkBm();
    bm.materializeFork(1, 0, "A", 3, 1001, (id) => `/w/br/${id}`);
    bm.report(1, "done", "方向完成", ["ev"], ["a.log"], [], 1002);
    const again = bm.report(1, "deadend", "重复", [], [], [], 1003);
    expect(again.ok).toBe(false);
    expect(again.message).toContain("已经以 done 结束");

    const lite = bm
      .alive()
      .filter((b) => b.id !== 0)
      .map((b) => ({
        id: b.id,
        parent: b.parent,
        scopeId: b.scopeId,
        label: b.label,
        cwd: b.cwd,
        forkSeq: b.forkSeq,
        messages: b.messages,
        status: b.status,
        active: b.active,
        result: b.result,
      }));
    const bm2 = new BranchManager(CFG, trunkMsgs(), 2000);
    bm2.restore(lite, 2000);
    const b2 = bm2.get(1)!;
    expect(b2.status).toBe("done");
    expect(b2.active).toBe(false);
    expect(b2.result?.summary).toBe("方向完成");
    expect(bm2.scopeStatus(b2.scopeId)).toBe("satisfied");
  });

  it("已 report 结束的 Attempt 不会被新消息重挂复活", () => {
    const bm = mkBm();
    bm.materializeFork(1, 0, "A", 3, 1001, (id) => `/w/br/${id}`);
    bm.report(1, "deadend", "走死", [], [], [], 1002);
    expect(bm.enqueueMessage(1, "有新信息")).toBe(true);
    const b1 = bm.get(1)!;
    expect(b1.status).toBe("deadend");
    expect(b1.active).toBe(false);
    expect(b1.messages.at(-1)?.content).toContain("有新信息");
  });

  it("spawnFromCheckpoint：从分裂前前缀开兄弟，不复制 source 失败尾部", () => {
    const bm = mkBm();
    bm.materializeFork(1, 0, "HTTP", 3, 1001, (id) => `/w/br/${id}`);
    const b1 = bm.get(1)!;
    b1.messages.push({ role: "user", content: "HTTP 全走死的失败轨迹" });
    bm.report(1, "deadend", "HTTP 走死", [], [], ["动态 trace"], 1002);
    const made = bm.spawnFromCheckpoint(1, "动态 trace", 1003, (id) => `/w/br/${id}`);
    expect(made).not.toBeNull();
    const b2 = made!.branch;
    expect(b2.id).toBe(2);
    expect(b2.parent).toBe(0);
    expect(b2.forkSeq).toBe(3);
    expect(b2.checkpointMessages.length).toBe(trunkMsgs().length);
    expect(b2.messages.some((m) => String(m.content ?? "").includes("HTTP 全走死"))).toBe(false);
    expect(String(b2.messages.at(-1)?.content)).toContain("动态 trace");
  });

  it("跨 Attempt 命令去重：同一条命令只提示其他 Attempt", () => {
    const bm = mkBm();
    bm.materializeFork(1, 0, "A", 3, 1001, (id) => `/w/br/${id}`);
    bm.materializeFork(2, 0, "B", 3, 1002, (id) => `/w/br/${id}`);
    bm.pushCmd(1, "nmap   -sT 10.0.0.1");
    expect(bm.commandSeenElsewhere(2, "nmap -sT 10.0.0.1")).toBe(1);
    expect(bm.commandSeenElsewhere(1, "nmap -sT 10.0.0.1")).toBeNull();
    expect(bm.commandSeenElsewhere(2, "ffuf -u http://x")).toBeNull();
  });
});

describe("共享写 / 重建记录", () => {
  it("state_change 记录 pending，recovered 后从待恢复列表移除", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "mutation_"));
    const w = new CorelogWriter(path.join(dir, "log.jsonl"), "t-mu");
    const bm = mkBm();
    bm.materializeFork(1, 0, "redis", 3, 1001, (id) => `/w/br/${id}`);
    const deps = { bm, corelog: w, ledger: null, now: () => 1000, log: () => {} };
    const done = dispatchBranchTool(
      "state_change",
      {
        action: "启动 redis 并写入 /tmp/cache",
        recovery: "redis-server --daemonize yes; 重新写 /tmp/cache",
      },
      deps,
      1,
      "m1",
    )!;
    expect(done.result).toContain("recorded mutation");
    const id = /recorded mutation (\S+)/.exec(done.result)?.[1] ?? "";
    expect(id).toContain("mu-");
    let pending = pendingMutations(readCorelog(w.path));
    expect(pending.length).toBe(1);
    expect(pending[0].scopeId).toBe(bm.get(1)!.scopeId);
    expect(pending[0].recovery).toContain("redis-server");

    const recovered = dispatchBranchTool(
      "state_change",
      { status: "recovered", id, action: "启动 redis 并写入 /tmp/cache" },
      deps,
      1,
      "m2",
    )!;
    expect(recovered.result).toContain("recovered mutation");
    pending = pendingMutations(readCorelog(w.path));
    expect(pending.length).toBe(0);
  });
});

describe("fork 优先调度", () => {
  it("fork 后新 Attempt 优先，其次父 Attempt", () => {
    const bm = mkBm();
    bm.materializeFork(1, 0, "A", 3, 1001, (id) => `/w/br/${id}`);
    expect(bm.get(0)!.priority).toBe(1);
    expect(bm.get(1)!.priority).toBe(2);
    expect(bm.pickExcept()?.id).toBe(1);
    expect(bm.pickExcept()?.id).toBe(0);
    // boost 只消费一次，之后回到 RR
    expect(bm.get(0)!.priority).toBe(0);
    expect(bm.get(1)!.priority).toBe(1); // 新 fork 两票，剩一票
  });
});

describe("并发调度公平性", () => {
  it("ranked 同优先级按 rr 轮转，不让高 id 新分支永久挨饿", () => {
    const bm = mkBm();
    bm.materializeFork(1, 0, "A", 3, 1001, (id) => `/w/br/${id}`);
    bm.materializeFork(2, 0, "B", 4, 1002, (id) => `/w/br/${id}`);
    for (const id of [0, 1, 2]) bm.get(id)!.priority = 0;
    const seen: number[] = [];
    for (let i = 0; i < 3; i++) {
      const first = bm.ranked()[0];
      seen.push(first.id);
      bm.noteScheduled(first.id);
    }
    expect(seen).toEqual([0, 1, 2]);
    // 刚跑过 branch1 后，branch0 仍在 inflight：下一个必须是 branch2，不能又选 branch1。
    bm.noteScheduled(1);
    expect(bm.ranked(new Set([0]))[0]?.id).toBe(2);
  });
});

describe("并发注入邮箱", () => {
  it("busy 时 enqueue 进 inbox，回合开始 drain 到 messages", () => {
    const bm = mkBm();
    bm.materializeFork(1, 0, "A", 3, 1001, (id) => `/w/br/${id}`);
    const b = bm.get(1)!;
    b.busy = true;
    expect(bm.enqueueMessage(1, "忙碌时广播")).toBe(true);
    expect(b.messages.some((m) => String(m.content).includes("忙碌时广播"))).toBe(false);
    expect(b.inbox.length).toBe(1);
    bm.drainInbox(1);
    expect(b.messages.at(-1)?.content).toContain("忙碌时广播");
    expect(b.inbox.length).toBe(0);
    // 空闲时直投
    b.busy = false;
    bm.enqueueMessage(1, "空闲广播");
    expect(b.messages.at(-1)?.content).toContain("空闲广播");
  });
});

describe("materializeFork 协议安全", () => {
  it("复制父上下文 + 尾部 append 方向指令（不动 system）", () => {
    const bm = mkBm();
    const made = bm.materializeFork(1, 0, "探测 redis", 3, 1001, (id) => `/w/br/${id}`);
    expect(made).not.toBeNull();
    const msgs = made!.branch.messages;
    // system 逐字节一致（缓存红线：不动前缀）
    expect(msgs[0]).toEqual(trunkMsgs()[0]);
    // 父消息完整拷贝 + 尾部 directive
    expect(msgs.length).toBe(trunkMsgs().length + 1);
    expect(msgs.at(-1)?.role).toBe("user");
    expect(String(msgs.at(-1)?.content)).toContain("分支 1");
    expect(String(msgs.at(-1)?.content)).toContain("探测 redis");
    expect(String(msgs.at(-1)?.content)).toContain("/w/br/1");
  });

  it("重复 materialize 同 id 无效（防重放）", () => {
    const bm = mkBm();
    expect(bm.materializeFork(1, 0, "A", 3, 1001, (id) => `/w/br/${id}`)).not.toBeNull();
    expect(bm.materializeFork(1, 0, "A", 3, 1002, (id) => `/w/br/${id}`)).toBeNull();
  });
});

describe("FlagLedger", () => {
  it("索引去重 + 进度表示 + 完成判定", () => {
    const led = new FlagLedger();
    led.setTotal(3);
    expect(led.add(1, 0)).toBe(true);
    expect(led.add(1, 1)).toBe(false); // 同 index 重复
    expect(led.add(3, 1)).toBe(true);
    expect(led.progress()).toBe("2/3 (idx:1,3)");
    expect(led.done()).toBe(false);
    led.add(2, 0);
    expect(led.done()).toBe(true);
  });

  it("extractFlagIndex 解析 submit 响应（含 null）", () => {
    const ok = '{"correct":true,"matched_flag_index":2,"total_flag_count":5}';
    expect(extractFlagIndex(ok)).toBe(2);
    const bad = '{"correct":false,"matched_flag_index":null}';
    expect(extractFlagIndex(bad)).toBeNull();
    expect(extractFlagIndex("409 duplicate")).toBeNull();
  });
});

describe("corelog", () => {
  it("写读一致 + 重启 seq 接续", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "corelog_"));
    const file = path.join(dir, "log.jsonl");
    const w = new CorelogWriter(file, "t-01");
    w.write({ branch: 0, kind: "core", note: "发现注入点" });
    w.write({ branch: 1, kind: "flag", flag_index: 1, correct_flag_count: 1, total_flag_count: 2 });
    const w2 = new CorelogWriter(file, "t-01"); // 模拟重启
    w2.write({ branch: 1, kind: "core", note: "继续" });
    const entries = readCorelog(file);
    expect(entries.map((e) => e.seq)).toEqual([1, 2, 3]);
    expect(entries[1].kind).toBe("flag");
  });

  it("buildAttemptLogView 按分支分组（交错写入 → 分组可读）", () => {
    const entries = [
      {
        ts: "2026-10-16 10:00:01",
        seq: 1,
        code: "t",
        branch: 0,
        kind: "core" as const,
        note: "A1",
      },
      {
        ts: "2026-10-16 10:00:02",
        seq: 2,
        code: "t",
        branch: 1,
        kind: "core" as const,
        note: "B1",
      },
      {
        ts: "2026-10-16 10:00:03",
        seq: 3,
        code: "t",
        branch: 0,
        kind: "core" as const,
        note: "A2",
      },
      {
        ts: "2026-10-16 10:00:04",
        seq: 4,
        code: "t",
        branch: 1,
        kind: "flag" as const,
        flag_index: 1,
      },
    ];
    const view = buildAttemptLogView(entries, {
      branches: new Map([[1, "探测 redis"]]),
    });
    // 分支 0 的两条连续、分支 1 的两条连续，不按全局时间交错
    expect(view.indexOf("A1")).toBeLessThan(view.indexOf("A2"));
    expect(view.indexOf("B1")).toBeLessThan(view.indexOf("[flag] idx=1"));
    expect(view).toContain("探测 redis");
    expect(view).toContain("Attempt 0（初始）");
  });
});

describe("卡塞信号与 planner 提醒", () => {
  it("evaluateStall 五信号：flag 主时钟一票 + 分支级任二", () => {
    const base = {
      sinceCoreSec: 0,
      stallSec: 300,
      flagSinceSec: 0,
      flagStallSec: 900,
      recentCmds: [] as string[],
    };
    expect(evaluateStall(base).count).toBe(0);
    expect(shouldIntervene(evaluateStall(base))).toBe(false);
    // ⓪ flag 主时钟：长时间没 flag 一票触发（以 flag 为核心的介入锚；
    //    即使分支刚记过卡——run3 记卡型空转靠它抓）
    const fs0 = evaluateStall({ ...base, flagSinceSec: 901 });
    expect(fs0.flagStall).toBe(true);
    expect(shouldIntervene(fs0)).toBe(true);
    expect(evaluateStall({ ...base, flagSinceSec: 800 }).flagStall).toBe(false);
    // ① 只停滞 → 1（不触发）
    const s1 = evaluateStall({ ...base, sinceCoreSec: 301 });
    expect(s1.count).toBe(1);
    expect(shouldIntervene(s1)).toBe(false);
    // ①+② 停滞+重复 → 触发
    const s12 = evaluateStall({
      ...base,
      sinceCoreSec: 301,
      recentCmds: ["nmap -sT x", "nmap -sT x", "nmap -sT x"],
    });
    expect(s12.count).toBe(2);
    expect(shouldIntervene(s12)).toBe(true);
    // ③ 近8次连接类硬失败≥3（幻觉目标空转）
    const hf = evaluateStall({ ...base, recentHardFails: [false, true, true, true] });
    expect(hf.hardFail).toBe(true);
    expect(hf.count).toBe(1);
    expect(evaluateStall({ ...base, recentHardFails: [true, true] }).hardFail).toBe(false);
    // ④ 连续 correct:false ≥2（幻觉提交）
    expect(evaluateStall({ ...base, wrongSubmits: 2 }).hallucSubmit).toBe(true);
    expect(evaluateStall({ ...base, wrongSubmits: 1 }).hallucSubmit).toBe(false);
    // ③+④：错提交+连接空转（无任何时间信号也触发）
    expect(
      shouldIntervene(
        evaluateStall({ ...base, wrongSubmits: 2, recentHardFails: [true, true, true] }),
      ),
    ).toBe(true);
    // 新 flag 后主时钟由调用方刷新（flagSinceSec 归零 → 不再一票）
    expect(shouldIntervene(evaluateStall({ ...base, flagSinceSec: 0 }))).toBe(false);
  });
  it("recentDigest 截断并标注 tool_calls", () => {
    const digest = recentDigest(
      [
        {
          role: "assistant",
          content: "x".repeat(500),
          tool_calls: [{ function: { name: "shell" } }],
        },
        { role: "tool", content: "output" },
      ],
      5,
    );
    expect(digest).toContain("[tool_calls: shell]");
    expect(digest.length).toBeLessThan(600);
  });
});

describe("records 分支重建", () => {
  /** 构造一个带 fork 的 conversation.jsonl。 */
  function writeConv(): string {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "branch_rec_"));
    const file = path.join(dir, "conversation.jsonl");
    const recs = [
      { ts: "t", seq: 1, code: "x", kind: "system", content: "sys", branch: 0 },
      { ts: "t", seq: 2, code: "x", kind: "user", content: "card", branch: 0 },
      {
        ts: "t",
        seq: 3,
        code: "x",
        kind: "assistant",
        output: "先侦察",
        tool_calls: [{ id: "c1", name: "shell", arguments: '{"command":"nmap"}' }],
        branch: 0,
      },
      {
        ts: "t",
        seq: 4,
        code: "x",
        kind: "tool",
        tool_call_id: "c1",
        result: "open 80",
        branch: 0,
      },
      {
        ts: "t",
        seq: 5,
        code: "x",
        kind: "user",
        branch: 1,
        kind_note: "fork_directive",
        parent_branch: 0,
        fork_seq: 4,
        fork_label: "redis 方向",
        content: "【分支 1 · 方向】你由分支 0 分出",
      },
      {
        ts: "t",
        seq: 6,
        code: "x",
        kind: "assistant",
        branch: 1,
        output: "看 redis",
        tool_calls: [{ id: "c2", name: "shell", arguments: '{"command":"redis-cli"}' }],
      },
      { ts: "t", seq: 7, code: "x", kind: "tool", branch: 1, tool_call_id: "c2", result: "PONG" },
      // fork 之后 trunk 继续跑的轮次（不得进入分支 1 的重建）
      {
        ts: "t",
        seq: 8,
        code: "x",
        kind: "assistant",
        branch: 0,
        output: "继续 web",
        tool_calls: [{ id: "c3", name: "shell", arguments: '{"command":"curl"}' }],
      },
      { ts: "t", seq: 9, code: "x", kind: "tool", branch: 0, tool_call_id: "c3", result: "200" },
    ];
    fs.writeFileSync(file, recs.map((r) => JSON.stringify(r)).join("\n"));
    return file;
  }

  it("trunk 重建排除其他分支记录", () => {
    const file = writeConv();
    const trunk = rebuildMessages(file);
    expect(trunk.length).toBe(6); // seq 1,2,3,4,8,9（分支 1 的 5,6,7 被排除）
    expect(trunk.filter((m) => m.role === "tool").length).toBe(2);
    expect(trunk.some((m) => String(m.content).includes("看 redis"))).toBe(false);
  });

  it("分支重建 = 父分支 fork_seq 快照 + 分支自身记录（fork 后父轮次不进入）", () => {
    const file = writeConv();
    const branches = rebuildBranches(file);
    expect(branches.length).toBe(1);
    const b1 = branches[0];
    expect(b1.id).toBe(1);
    expect(b1.parent).toBe(0);
    expect(b1.forkSeq).toBe(4);
    expect(b1.label).toBe("redis 方向");
    // sys+card+asst(c1)+tool(c1) = 4（seq 8/9 的 trunk 轮次不得出现）
    expect(b1.messages.length).toBe(4 + 1 /*directive*/ + 2 /*asst+tool*/);
    expect(b1.messages[0].role).toBe("system");
    expect(String(b1.messages[4].content)).toContain("分支 1 · 方向");
    expect(b1.messages.some((m) => String(m.content).includes("继续 web"))).toBe(false);
    expect(b1.checkpointMessages.length).toBe(4); // 父前缀快照（无 directive/后续）
    expect(b1.checkpointMessages.some((m) => String(m.content).includes("继续 web"))).toBe(false);
  });

  it("嵌套 fork：孙分支只拿祖父 fork_seq 内的快照", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "nest_rec_"));
    const file = path.join(dir, "conversation.jsonl");
    const recs = [
      { ts: "t", seq: 1, code: "x", kind: "system", content: "sys", branch: 0 },
      { ts: "t", seq: 2, code: "x", kind: "user", content: "card", branch: 0 },
      {
        ts: "t",
        seq: 3,
        code: "x",
        kind: "user",
        branch: 1,
        kind_note: "fork_directive",
        parent_branch: 0,
        fork_seq: 2,
        content: "【分支 1】",
      },
      { ts: "t", seq: 4, code: "x", kind: "assistant", branch: 1, output: "b1 work" },
      // fork 孙分支（从分支 1，fork_seq=4）之后分支 1 又跑了一轮
      {
        ts: "t",
        seq: 5,
        code: "x",
        kind: "user",
        branch: 2,
        kind_note: "fork_directive",
        parent_branch: 1,
        fork_seq: 4,
        content: "【分支 2】",
      },
      { ts: "t", seq: 6, code: "x", kind: "assistant", branch: 1, output: "b1 more（不该进 b2）" },
      { ts: "t", seq: 7, code: "x", kind: "assistant", branch: 2, output: "b2 work" },
    ];
    fs.writeFileSync(file, recs.map((r) => JSON.stringify(r)).join("\n"));
    const branches = rebuildBranches(file);
    const b2 = branches.find((b) => b.id === 2)!;
    expect(b2.messages.some((m) => String(m.content).includes("b1 work"))).toBe(true);
    expect(b2.messages.some((m) => String(m.content).includes("不该进 b2"))).toBe(false);
    expect(b2.messages.at(-1)?.content).toBe("b2 work");
  });

  it("坏记录（孤儿 tool/半截 tool_calls）被裁剪", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "bad_rec_"));
    const file = path.join(dir, "conversation.jsonl");
    const recs = [
      { ts: "t", seq: 1, code: "x", kind: "system", content: "sys", branch: 0 },
      { ts: "t", seq: 2, code: "x", kind: "user", content: "card", branch: 0 },
      {
        ts: "t",
        seq: 3,
        code: "x",
        kind: "assistant",
        branch: 0,
        tool_calls: [{ id: "cx", name: "shell", arguments: "{}" }], // 结果缺失
      },
      { ts: "t", seq: 4, code: "x", kind: "tool", tool_call_id: "orphan", result: "x", branch: 0 },
    ];
    fs.writeFileSync(file, recs.map((r) => JSON.stringify(r)).join("\n"));
    const msgs = rebuildMessages(file);
    expect(msgs.length).toBe(2); // 半截 assistant 与孤儿 tool 都被裁掉
  });

  it("中段孤儿 assistant（tool_calls 无结果、其后还有别的消息）截断后续（防 400）", () => {
    // arena 事故形态：多写者交错/半截段重建 → 中段出现"有 tool_calls 无紧跟结果"的
    // assistant，旧逻辑只裁尾部 → 漏过去发给 DeepSeek → 400 abandoned。
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "mid_orphan_"));
    const file = path.join(dir, "conversation.jsonl");
    const recs = [
      { ts: "t", seq: 1, code: "x", kind: "system", content: "sys", branch: 0 },
      { ts: "t", seq: 2, code: "x", kind: "user", content: "card", branch: 0 },
      {
        ts: "t",
        seq: 3,
        code: "x",
        kind: "assistant",
        branch: 0,
        tool_calls: [{ id: "cx", name: "shell", arguments: "{}" }], // 结果缺失（中段孤儿）
      },
      // 其后还有完整配对的正常轮次（旧逻辑会保留它们，导致 cx 悬空 → 400）
      {
        ts: "t",
        seq: 4,
        code: "x",
        kind: "assistant",
        branch: 0,
        output: "继续",
        tool_calls: [{ id: "cy", name: "shell", arguments: "{}" }],
      },
      { ts: "t", seq: 5, code: "x", kind: "tool", branch: 0, tool_call_id: "cy", result: "ok" },
      { ts: "t", seq: 6, code: "x", kind: "assistant", branch: 0, output: "总结" },
    ];
    fs.writeFileSync(file, recs.map((r) => JSON.stringify(r)).join("\n"));
    const msgs = rebuildMessages(file);
    // 从孤儿 assistant（seq 3）截断：只剩 sys + card
    expect(msgs.length).toBe(2);
    expect(msgs.at(-1)?.content).toBe("card");
  });
});

describe("log 检查方向归 sys（不逐次塞工具结果）", () => {
  it("工具结果只回确认事实；六要素/置信要求/黑板路径在 sys extras", () => {
    const bm = mkBm();
    const out = dispatchBranchTool(
      "log",
      { note: "方法:探测 /download 得 validator ELF(已验证)" },
      { bm, corelog: null, ledger: null, now: () => 1000, log: () => {} },
      0,
      "c1",
    );
    expect(out).not.toBeNull();
    // 工具结果 = 确认事实，不带教学检查行（arena 实测 8/8 结果重复下发——归 sys）
    expect(out!.result).toContain("logged (branch 0)");
    expect(out!.result).not.toContain("六要素自查");
    expect(out!.result).not.toContain("齐了吗");
    expect(out!.result).not.toContain("判断带置信");
    // 信心要求 + 自动记黑板声明常驻 sys（fork / planner两种模式都覆盖）
    expect(FORK_PROMPT_EXTRA).toContain("判断明确写信心");
    expect(FORK_PROMPT_EXTRA).toContain("自动记入黑板");
    expect(FORK_PROMPT_EXTRA).toContain("log.jsonl");
    // 六要素一行在基础 sys（log 工具在场时）
    expect(systemPromptWithExtras(true)).toContain("报告六要素");
    expect(systemPromptWithExtras(true).startsWith(FORK_SYSTEM_HINT)).toBe(true);
    expect(systemPromptWithExtras(true)).toContain("先看：多 Attempt 协作");
    expect(systemPromptWithExtras(false)).not.toContain("先看：多 Attempt 协作");
  });
});
